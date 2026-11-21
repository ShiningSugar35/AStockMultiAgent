"""Bounded report samples and reproducible, source-qualified forecast aggregates."""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.network_ownership import owned_network_request
from astock.core.object_store import ObjectStore
from astock.core.request_budget import current_request_budget
from astock.core.state import StateStore
from astock.providers.http_resilience import configure_owned_http_client
from astock.schemas import Market, SourceSnapshot

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ENDPOINT = "https://reportapi.eastmoney.com/report/list"


class SampledForecastObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    observation_id: str
    company_id: str
    market: Market
    metric: Literal["EPS"] = "EPS"
    target_fiscal_year: int = Field(ge=1990, le=2100)
    currency: Literal["CNY"] = "CNY"
    unit: Literal["CNY_PER_SHARE"] = "CNY_PER_SHARE"
    value: Decimal = Field(allow_inf_nan=False)
    institution_id: str
    institution_name: str
    report_id: str
    report_url: str
    report_title: str | None = Field(default=None, max_length=2000)
    author_text: str | None = Field(default=None, max_length=1000)
    reported_rating: str | None = Field(default=None, max_length=160)
    provider_normalized_rating: str | None = Field(default=None, max_length=160)
    report_pdf_hash: None = None
    report_page: None = None
    report_pdf_status: Literal["URL_ONLY_NOT_CAPTURED"] = "URL_ONLY_NOT_CAPTURED"
    published_at: AwareDatetime
    publication_time_precision: Literal["DATE", "SECOND"]
    available_to_system_at: AwareDatetime
    source_snapshot_id: str
    source_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_group: Literal["EASTMONEY"] = "EASTMONEY"
    target_year_basis: Literal["UPSTREAM_RESPONSE_CURRENT_YEAR"] = "UPSTREAM_RESPONSE_CURRENT_YEAR"
    epistemic_type: Literal["FORECAST"] = "FORECAST"

    @model_validator(mode="after")
    def valid_time(self) -> SampledForecastObservation:
        if self.published_at > self.available_to_system_at:
            raise ValueError("forecast publication follows capture")
        return self


class SampledForecastCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["sampled-forecast-capture-v1"] = "sampled-forecast-capture-v1"
    capture_id: str
    company_id: str
    market: Market
    window_start: date
    window_end: date
    available_to_system_at: AwareDatetime
    source_snapshot_ids: tuple[str, ...]
    observations: tuple[SampledForecastObservation, ...]
    pages_read: int = Field(ge=1, le=3)
    rows_seen: int = Field(ge=0)
    rejected_rows: int = Field(ge=0)
    missing_estimate_count: int = Field(ge=0)
    truncated: bool
    source_group: Literal["EASTMONEY"] = "EASTMONEY"
    coverage: Literal["BOUNDED_SAMPLE_NOT_FULL_CONSENSUS"] = "BOUNDED_SAMPLE_NOT_FULL_CONSENSUS"
    historical_vintage_verified: Literal[False] = False


class SampledForecastAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["sampled-forecast-aggregate-v1"] = "sampled-forecast-aggregate-v1"
    company_id: str
    market: Market
    metric: Literal["EPS"] = "EPS"
    target_fiscal_year: int
    currency: Literal["CNY"] = "CNY"
    unit: Literal["CNY_PER_SHARE"] = "CNY_PER_SHARE"
    as_of: AwareDatetime
    window_start: date
    window_end: date
    source_groups: tuple[str, ...]
    capture_object_hashes: tuple[str, ...]
    observation_ids: tuple[str, ...]
    raw_sample_count: int
    sample_count: int
    institution_count: int
    conflicting_institutions: tuple[str, ...]
    mean: Decimal | None
    median: Decimal | None
    standard_deviation: Decimal | None
    minimum: Decimal | None
    maximum: Decimal | None
    coverage: Literal["BOUNDED_SAMPLE_NOT_FULL_CONSENSUS"] = "BOUNDED_SAMPLE_NOT_FULL_CONSENSUS"
    historical_vintage_verified: Literal[False] = False

    @property
    def artifact_id(self) -> str:
        return "sampled-forecast-aggregate:" + sha256_bytes(
            canonical_json_bytes(self.model_dump(mode="json"))
        )


class EastMoneyForecastProvider:
    """Same upstream group as AKShare's report adapter; no second independent vote."""

    provider_id = "eastmoney-forecasts"
    recorded_supported = False

    def __init__(
        self, object_store: ObjectStore, state: StateStore, client: httpx.Client | None = None
    ) -> None:
        self.objects, self.state = object_store, state
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        configure_owned_http_client(self.client)

    def capture(
        self, company_id: str, market: Market, start: date, end: date,
        *, max_pages: int = 2, page_size: int = 50,
    ) -> SampledForecastCapture:
        if not re.fullmatch(r"\d{6}", company_id) or market not in {
            Market.XSHG, Market.XSHE, Market.BJSE
        }:
            raise ValueError("forecast request requires one A-share identity")
        if not (0 <= (end - start).days <= 366) or end > datetime.now(_SHANGHAI).date():
            raise ValueError("forecast window must be bounded and not in the future")
        if not 1 <= max_pages <= 3 or not 1 <= page_size <= 100:
            raise ValueError("forecast page budget exceeded")
        deadline = time.monotonic() + 20
        snapshots: list[SourceSnapshot] = []
        observations: list[SampledForecastObservation] = []
        rows_seen = rejected = missing = 0
        truncated = False
        response_year: int | None = None
        for page in range(1, max_pages + 1):
            params = {
                "industryCode": "*", "industry": "*", "rating": "*", "ratingChange": "*",
                "beginTime": start.isoformat(), "endTime": end.isoformat(), "pageSize": page_size,
                "pageNo": page, "p": page, "pageNum": page, "pageNumber": page,
                "fields": "", "qType": "0", "orgCode": "", "code": company_id, "rcode": "",
            }
            body, url = self._read_http_page(params, deadline)
            raw = self.objects.put_bytes(bytes(body))
            observed = datetime.now(UTC)
            snapshot = SourceSnapshot(
                snapshot_id=f"{self.provider_id}:{content_hash(params)}:{raw.sha256}",
                source_id=self.provider_id,
                object_sha256=raw.sha256, fetched_at=observed, available_to_system_at=observed,
                source_url=url, mime="application/json", byte_size=raw.byte_size,
            )
            existing = self.state.get_snapshot(snapshot.snapshot_id)
            if existing is None:
                self.state.register_snapshot(snapshot)
            else:
                snapshot = existing
            payload = json.loads(body)
            year = _response_year(payload)
            if response_year is not None and response_year != year:
                raise ValueError("forecast response-year changed across pages")
            response_year = year
            parsed, invalid, absent = normalize_forecast_page(
                payload, snapshot=snapshot, company_id=company_id, market=market,
                start=start, end=end,
            )
            data = payload["data"]
            if len(data) > page_size:
                raise ValueError("upstream ignored forecast page budget")
            rows_seen += len(data)
            rejected += invalid
            missing += absent
            snapshots.append(snapshot)
            observations.extend(parsed)
            raw_page_count = payload.get("TotalPage", 1)
            if isinstance(raw_page_count, bool) or not re.fullmatch(
                r"\d{1,7}", str(raw_page_count)
            ):
                raise ValueError("invalid forecast page count")
            total_pages = int(raw_page_count)
            truncated = total_pages > page
            if not truncated or not data:
                break
        identity = {
            "company_id": company_id, "market": market, "start": start, "end": end,
            "snapshots": [item.snapshot_id for item in snapshots],
        }
        result = SampledForecastCapture(
            capture_id="sampled-forecast:" + content_hash(identity), company_id=company_id,
            market=market, window_start=start, window_end=end,
            available_to_system_at=max(item.available_to_system_at for item in snapshots),
            source_snapshot_ids=tuple(item.snapshot_id for item in snapshots),
            observations=tuple(observations), pages_read=len(snapshots), rows_seen=rows_seen,
            rejected_rows=rejected, missing_estimate_count=missing, truncated=truncated,
        )
        ref = self.objects.put_json(result.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=result.capture_id, artifact_type="SampledForecastCapture",
            schema_version=result.schema_version, object_hash=ref.sha256,
            input_hashes=[item.object_sha256 for item in snapshots],
        )
        return result


    def _read_http_page(
        self, params: dict[str, str | int], deadline: float,
    ) -> tuple[bytes, str]:
        budget = current_request_budget()
        timeout = deadline - time.monotonic()
        if budget is not None:
            timeout = min(timeout, budget.remaining_seconds())
        if timeout <= 0:
            raise TimeoutError("forecast collection deadline exhausted")
        body = bytearray()
        with owned_network_request(
            self.state, self.provider_id, _ENDPOINT, timeout_seconds=timeout,
        ), self.client.stream(
            "GET", _ENDPOINT, params=params, timeout=timeout, follow_redirects=False,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=65_536):
                if budget is not None and budget.remaining_seconds() <= 0:
                    raise TimeoutError("forecast request deadline exhausted")
                body.extend(chunk)
                if len(body) > 2_097_152 or time.monotonic() > deadline:
                    raise ValueError("forecast response exceeds byte/time budget")
            return bytes(body), str(response.request.url)


def _response_year(payload: Any) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("forecast page must have a report list")
    year = payload.get("currentYear")
    if isinstance(year, bool) or not re.fullmatch(r"20\d{2}", str(year)):
        raise ValueError("forecast target year requires explicit upstream currentYear")
    return int(str(year))


def _optional_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text and len(text) <= limit else None


def normalize_forecast_page(
    payload: Any, *, snapshot: SourceSnapshot, company_id: str, market: Market,
    start: date, end: date,
) -> tuple[list[SampledForecastObservation], int, int]:
    year = _response_year(payload)
    rows: list[SampledForecastObservation] = []
    rejected = missing = 0
    for row in payload["data"]:
        try:
            if not isinstance(row, dict) or str(row.get("stockCode")) != company_id:
                raise ValueError("forecast company mismatch")
            raw_report, raw_institution = row.get("infoCode"), row.get("orgCode")
            if any(
                value is None or isinstance(value, bool)
                for value in (raw_report, raw_institution)
            ):
                raise ValueError("report/institution identity missing")
            report_id, institution = str(raw_report), str(raw_institution)
            if not all(
                re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value) for value in (report_id, institution)
            ):
                raise ValueError("report/institution identity missing")
            publication = str(row["publishDate"])
            published = datetime.fromisoformat(publication)
            precision = "DATE" if len(publication) == 10 else "SECOND"
            if published.tzinfo is None:
                published = published.replace(tzinfo=_SHANGHAI)
            if not start <= published.astimezone(_SHANGHAI).date() <= end:
                raise ValueError("forecast row outside requested window")
            if published > snapshot.available_to_system_at:
                raise ValueError("future report")
            row_observations: list[SampledForecastObservation] = []
            for offset, key in enumerate(
                ("predictThisYearEps", "predictNextYearEps", "predictNextTwoYearEps")
            ):
                raw_value = row.get(key)
                if raw_value is None or raw_value in ("", "-", "--"):
                    missing += 1
                    continue
                if isinstance(raw_value, bool):
                    raise ValueError("boolean forecast")
                value = Decimal(str(raw_value))
                if not value.is_finite():
                    raise ValueError("non-finite forecast")
                row_hash = content_hash(row)
                identity = {
                    "snapshot": snapshot.snapshot_id, "row": row_hash, "metric": "EPS",
                    "target_year": year + offset,
                }
                row_observations.append(SampledForecastObservation(
                    observation_id=content_hash(identity), company_id=company_id, market=market,
                    target_fiscal_year=year + offset, value=value,
                    institution_id=institution,
                    institution_name=str(row.get("orgSName", institution)),
                    report_id=report_id,
                    report_url=f"https://pdf.dfcfw.com/pdf/H3_{report_id}_1.pdf",
                    report_title=_optional_text(row.get("title"), 2000),
                    author_text=_optional_text(row.get("researcher"), 1000),
                    reported_rating=_optional_text(row.get("sRatingName"), 160),
                    provider_normalized_rating=_optional_text(row.get("emRatingName"), 160),
                    published_at=published, publication_time_precision=precision,
                    available_to_system_at=snapshot.available_to_system_at,
                    source_snapshot_id=snapshot.snapshot_id,
                    source_object_hash=snapshot.object_sha256,
                    row_hash=row_hash,
                ))
            rows.extend(row_observations)
        except (KeyError, TypeError, ValueError, InvalidOperation):
            rejected += 1
    return rows, rejected, missing


def load_forecast_capture(
    state: StateStore, objects: ObjectStore, artifact_id: str, *,
    company_id: str, market: Market, as_of: datetime,
) -> SampledForecastCapture:
    """Read one registered capture and revalidate its raw versions without writing."""
    record = state.artifact_record(artifact_id)
    if not record or record["type"] != "SampledForecastCapture":
        raise ValueError("forecast capture is not registered")
    digest = str(record["object_hash"])
    try:
        raw = objects.get_bytes(digest)
    except StorageError as exc:
        raise ValueError("forecast capture object mismatch") from exc
    capture = SampledForecastCapture.model_validate_json(raw)
    if raw != canonical_json_bytes(capture.model_dump(mode="json")):
        raise ValueError("forecast capture object mismatch")
    if capture.capture_id != artifact_id:
        raise ValueError("forecast capture registration identity mismatch")
    _validate_forecast_capture(
        capture, state=state, objects=objects, company_id=company_id,
        market=market, as_of=as_of,
    )
    return capture


def _validate_forecast_capture(
    capture: SampledForecastCapture, *, state: StateStore, objects: ObjectStore,
    company_id: str, market: Market, as_of: datetime,
) -> None:
    if as_of.tzinfo is None or capture.window_start > capture.window_end:
        raise ValueError("forecast capture requires an aware cutoff and valid window")
    if capture.company_id != company_id or capture.market is not market:
        raise ValueError("forecast capture identity mismatch")
    if capture.available_to_system_at > as_of:
        raise ValueError("forecast capture was unavailable at cutoff")
    verified: list[SampledForecastObservation] = []
    source_times: list[datetime] = []
    if not capture.source_snapshot_ids or len(set(capture.source_snapshot_ids)) != len(
        capture.source_snapshot_ids
    ):
        raise ValueError("forecast source snapshot set is empty or duplicated")
    for snapshot_id in capture.source_snapshot_ids:
        snapshot = state.get_snapshot(snapshot_id)
        if (
            snapshot is None or snapshot.source_id != "eastmoney-forecasts"
            or snapshot.fetch_status.value != "SUCCEEDED"
            or not objects.verify(snapshot.object_sha256)
        ):
            raise ValueError("forecast raw source is unavailable or has the wrong provider")
        if snapshot.available_to_system_at > as_of:
            raise ValueError("forecast raw source was unavailable at cutoff")
        source_times.append(snapshot.available_to_system_at)
        parsed, _, _ = normalize_forecast_page(
            json.loads(objects.get_bytes(snapshot.object_sha256)), snapshot=snapshot,
            company_id=company_id, market=market, start=capture.window_start,
            end=capture.window_end,
        )
        verified.extend(parsed)
    if capture.available_to_system_at != max(source_times):
        raise ValueError("forecast capture visibility differs from its raw sources")
    if tuple(verified) != capture.observations or any(
        item.available_to_system_at > as_of for item in capture.observations
    ):
        raise ValueError("forecast values differ from their raw capture")


def select_latest_forecasts(
    observations: list[SampledForecastObservation],
) -> tuple[list[SampledForecastObservation], tuple[str, ...]]:
    """Select one latest, conflict-free version per institution from verified observations."""
    identities = {
        (item.company_id, item.market, item.target_fiscal_year,
         item.metric, item.currency, item.unit)
        for item in observations
    }
    if len(identities) > 1:
        raise ValueError("forecast selection requires one company, fiscal year and metric")
    by_institution: dict[str, list[SampledForecastObservation]] = defaultdict(list)
    for item in observations:
        by_institution[item.institution_id].append(item)
    selected: list[SampledForecastObservation] = []
    conflicts: list[str] = []
    for institution, items in sorted(by_institution.items()):
        latest_publication = max(item.published_at for item in items)
        latest = [item for item in items if item.published_at == latest_publication]
        # Repeated observations of one report are versions, not extra votes.
        by_report: dict[str, SampledForecastObservation] = {}
        version_conflict = False
        for report_id in sorted({item.report_id for item in latest}):
            report_versions = [item for item in latest if item.report_id == report_id]
            latest_capture = max(item.available_to_system_at for item in report_versions)
            current_versions = [
                item for item in report_versions if item.available_to_system_at == latest_capture
            ]
            if len({item.value for item in current_versions}) != 1:
                version_conflict = True
                break
            by_report[report_id] = max(current_versions, key=lambda x: x.observation_id)
        if version_conflict or len({item.value for item in by_report.values()}) != 1:
            conflicts.append(institution)
            continue
        selected.append(max(
            by_report.values(), key=lambda x: (x.available_to_system_at, x.observation_id)
        ))
    return selected, tuple(conflicts)


def aggregate_forecasts(
    captures: list[SampledForecastCapture], *, company_id: str, market: Market,
    target_fiscal_year: int, as_of: datetime, window_start: date, window_end: date,
    state: StateStore, objects: ObjectStore, persist: bool = True,
) -> SampledForecastAggregate:
    """One latest estimate per institution; conflicting latest versions are excluded."""
    if as_of.tzinfo is None or window_start > window_end:
        raise ValueError("forecast aggregate requires an aware cutoff and valid window")
    candidates: dict[str, SampledForecastObservation] = {}
    capture_hashes: list[str] = []
    for capture in captures:
        verified = load_forecast_capture(
            state, objects, capture.capture_id, company_id=company_id, market=market, as_of=as_of,
        )
        if verified != capture:
            raise ValueError("forecast capture object mismatch")
        capture_hashes.append(sha256_bytes(canonical_json_bytes(verified.model_dump(mode="json"))))
        for item in capture.observations:
            if item.target_fiscal_year == target_fiscal_year and (
                window_start <= item.published_at.astimezone(_SHANGHAI).date() <= window_end
            ):
                candidates[item.observation_id] = item
    selected, conflicts = select_latest_forecasts(list(candidates.values()))
    values = sorted(item.value for item in selected)
    count = len(values)
    mean = sum(values, Decimal(0)) / count if count else None
    median = ((values[count // 2] if count % 2 else
               (values[count // 2 - 1] + values[count // 2]) / 2) if count else None)
    deviation = (
        (sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(count - 1)).sqrt()
        if count > 1 and mean is not None else None
    )
    result = SampledForecastAggregate(
        company_id=company_id, market=market, target_fiscal_year=target_fiscal_year,
        as_of=as_of, window_start=window_start, window_end=window_end,
        source_groups=tuple(sorted({item.source_group for item in selected})),
        capture_object_hashes=tuple(sorted(set(capture_hashes))),
        observation_ids=tuple(sorted(item.observation_id for item in selected)),
        raw_sample_count=len(candidates), sample_count=count, institution_count=count,
        conflicting_institutions=tuple(conflicts), mean=mean, median=median,
        standard_deviation=deviation, minimum=values[0] if count else None,
        maximum=values[-1] if count else None,
    )
    if persist:
        ref = objects.put_json(result.model_dump(mode="json"))
        state.register_artifact(
            artifact_id=result.artifact_id,
            artifact_type="SampledForecastAggregate", schema_version=result.schema_version,
            object_hash=ref.sha256, input_hashes=list(result.capture_object_hashes),
        )
    return result
