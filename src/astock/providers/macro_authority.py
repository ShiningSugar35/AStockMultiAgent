"""Recorded-first official macro releases with durable PIT semantics.

Recorded fixtures are frozen official captures.  Replay preserves the original
system-availability timestamp and registers typed PIT metadata.  Live extractors
remain deliberately unavailable until an authority-specific parser is proven.
"""

from __future__ import annotations

import calendar
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astock.core.hashing import content_hash
from astock.pit.repository import PointInTimeRepository
from astock.pit.service import PointInTimeService
from astock.providers.base import HttpProviderBase
from astock.providers.http_resilience import HttpClientLike
from astock.schemas import (
    AvailabilityBasis,
    FetchStatus,
    PointInTimeStatus,
    SourceSnapshot,
)


class MacroAuthorityReleaseError(ValueError):
    """Schema/coverage failure with the raw snapshot attached."""

    def __init__(self, message: str, *, snapshot_id: str | None = None) -> None:
        super().__init__(message)
        self.snapshot_id = snapshot_id


class NbsStatisticalReleaseProvider(HttpProviderBase):
    """National Bureau of Statistics macro-economic indicator provider.

    Fetches indicator data from the NBS official website (stats.gov.cn).
    Each indicator has a fixed publication schedule and may be revised
    multiple times.  The adapter records observation period, publication
    date, revision version, and available_to_system_at for PIT correctness.
    """

    provider_id = "nbs-statistical-release"
    base_url = "https://data.stats.gov.cn/easyquery.htm"

    def __init__(
        self,
        objects: Any,
        state: Any,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_indicator(
        self,
        indicator_code: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], Any]:
        if not live:
            return self._recorded_indicator(indicator_code)
        raise MacroAuthorityReleaseError(
            "Live NBS indicator fetch not yet implemented; use recorded mode"
        )

    def _recorded_indicator(
        self,
        indicator_code: str,
    ) -> tuple[dict[str, object], Any]:
        path = (self.fixture_root / f"nbs_{indicator_code}.json").resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise MacroAuthorityReleaseError(
                f"Missing recorded NBS fixture for indicator: {indicator_code}"
            )
        return _recorded_macro_release(self, path, self.provider_id)


class PbocMonetaryPolicyReleaseProvider(HttpProviderBase):
    """People's Bank of China monetary policy indicator provider.

    Fetches monetary policy data from the PBOC official website (pbc.gov.cn).
    Each indicator has a fixed publication schedule and may be revised.
    """

    provider_id = "pboc-monetary-policy-release"
    base_url = "https://www.pbc.gov.cn"

    def __init__(
        self,
        objects: Any,
        state: Any,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_indicator(
        self,
        indicator_code: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], Any]:
        if not live:
            return self._recorded_indicator(indicator_code)
        raise MacroAuthorityReleaseError(
            "Live PBOC indicator fetch not yet implemented; use recorded mode"
        )

    def _recorded_indicator(
        self,
        indicator_code: str,
    ) -> tuple[dict[str, object], Any]:
        path = (self.fixture_root / f"pboc_{indicator_code}.json").resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise MacroAuthorityReleaseError(
                f"Missing recorded PBOC fixture for indicator: {indicator_code}"
            )
        return _recorded_macro_release(self, path, self.provider_id)


class MofFiscalPolicyReleaseProvider(HttpProviderBase):
    """Ministry of Finance fiscal policy indicator provider.

    Fetches fiscal policy data from the MOF official website (mof.gov.cn).
    """

    provider_id = "mof-fiscal-policy-release"
    base_url = "https://www.mof.gov.cn"

    def __init__(
        self,
        objects: Any,
        state: Any,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_indicator(
        self,
        indicator_code: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], Any]:
        if not live:
            return self._recorded_indicator(indicator_code)
        raise MacroAuthorityReleaseError(
            "Live MOF indicator fetch not yet implemented; use recorded mode"
        )

    def _recorded_indicator(
        self,
        indicator_code: str,
    ) -> tuple[dict[str, object], Any]:
        path = (self.fixture_root / f"mof_{indicator_code}.json").resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise MacroAuthorityReleaseError(
                f"Missing recorded MOF fixture for indicator: {indicator_code}"
            )
        return _recorded_macro_release(self, path, self.provider_id)


class NdrcPricingPolicyReleaseProvider(HttpProviderBase):
    """National Development and Reform Commission pricing policy provider.

    Fetches pricing policy data from the NDRC official website (ndrc.gov.cn).
    """

    provider_id = "ndrc-pricing-policy-release"
    base_url = "https://www.ndrc.gov.cn"

    def __init__(
        self,
        objects: Any,
        state: Any,
        fixture_root: Path,
        *,
        client: HttpClientLike | None = None,
    ) -> None:
        super().__init__(objects, state, client=client)
        self.fixture_root = fixture_root.resolve()

    def fetch_indicator(
        self,
        indicator_code: str,
        *,
        live: bool = False,
    ) -> tuple[dict[str, object], Any]:
        if not live:
            return self._recorded_indicator(indicator_code)
        raise MacroAuthorityReleaseError(
            "Live NDRC indicator fetch not yet implemented; use recorded mode"
        )

    def _recorded_indicator(
        self,
        indicator_code: str,
    ) -> tuple[dict[str, object], Any]:
        path = (self.fixture_root / f"ndrc_{indicator_code}.json").resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise MacroAuthorityReleaseError(
                f"Missing recorded NDRC fixture for indicator: {indicator_code}"
            )
        return _recorded_macro_release(self, path, self.provider_id)


_ALLOWED_MACRO_HOSTS = {
    "nbs-statistical-release": "stats.gov.cn",
    "pboc-monetary-policy-release": "pbc.gov.cn",
    "mof-fiscal-policy-release": "mof.gov.cn",
    "ndrc-pricing-policy-release": "ndrc.gov.cn",
}


def _recorded_macro_release(
    provider: HttpProviderBase,
    path: Path,
    provider_id: str,
) -> tuple[dict[str, object], SourceSnapshot]:
    content = path.read_bytes()
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MacroAuthorityReleaseError("Recorded macro fixture is not JSON") from exc
    if not isinstance(payload, dict):
        raise MacroAuthorityReleaseError("Recorded macro fixture root must be an object")
    _validate_macro_release(payload, provider_id)
    available = _aware_datetime(payload["available_to_system_at"])
    source_url = str(payload["source_url"])
    indicator = str(payload["indicator_code"])
    observation_period = str(payload["observation_period"])
    revision = int(str(payload["revision_version"]))
    source_id = f"macro:{provider_id}:{indicator}:{observation_period}:v{revision}"
    repository = PointInTimeRepository(provider.state)
    predecessor = None
    if revision > 1:
        predecessor = f"macro:{provider_id}:{indicator}:{observation_period}:v{revision - 1}"
        if repository.get_by_source(predecessor) is None:
            raise MacroAuthorityReleaseError(
                f"Macro revision predecessor is not recorded: {predecessor}"
            )
    object_ref = provider.object_store.put_bytes(content)
    snapshot = SourceSnapshot(
        created_at=available,
        snapshot_id=f"{provider_id}:recorded:{object_ref.sha256}",
        source_id=provider_id,
        object_sha256=object_ref.sha256,
        fetched_at=available,
        available_to_system_at=available,
        source_url=source_url,
        mime="application/json",
        byte_size=object_ref.byte_size,
        headers_hash=content_hash({"mode": "RECORDED_OFFICIAL_MACRO"}),
        fetch_status=FetchStatus.SUCCEEDED,
        rights_status="PUBLIC_OFFICIAL_AUTHORITY",
    )
    provider.state.register_snapshot(snapshot)
    PointInTimeService(
        repository,
        provider.state,
        provider.object_store,
    ).create(
        source_id=source_id,
        source_snapshot_id=snapshot.snapshot_id,
        period_end=_observation_period_end(observation_period),
        published_at=None,
        effective_at=None,
        ingested_at=available,
        available_to_system_at=available,
        revised_at=available if revision > 1 else None,
        supersedes_source_id=predecessor,
        point_in_time_status=PointInTimeStatus.CERTIFIED,
        availability_basis=AvailabilityBasis.FETCH_OBSERVED,
    )
    return payload, snapshot


def _aware_datetime(raw: object) -> datetime:
    if not isinstance(raw, str):
        raise MacroAuthorityReleaseError("Macro availability timestamp is invalid")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MacroAuthorityReleaseError("Macro availability timestamp is invalid") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise MacroAuthorityReleaseError("Macro availability timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _observation_period_end(raw: str) -> date:
    if len(raw) == 7 and raw[4:6] == "-Q" and raw[-1] in "1234":
        year = int(raw[:4])
        quarter = int(raw[-1])
        month = quarter * 3
        return date(year, month, calendar.monthrange(year, month)[1])
    if len(raw) == 7 and raw[4] == "-":
        year, month = (int(value) for value in raw.split("-"))
        return date(year, month, calendar.monthrange(year, month)[1])
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise MacroAuthorityReleaseError(
            f"Unsupported macro observation period: {raw}"
        ) from exc


def _validate_macro_release(payload: dict[str, object], provider_id: str) -> None:
    """Validate the macro-economic release schema."""
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.startswith("macro-release-v"):
        raise MacroAuthorityReleaseError(
            f"Invalid macro release schema version: {schema_version}"
        )
    source = payload.get("_astock_source")
    if not isinstance(source, str) or source != provider_id:
        raise MacroAuthorityReleaseError(
            f"Macro release source mismatch: expected {provider_id}, got {source}"
        )
    indicator_code = payload.get("indicator_code")
    if not isinstance(indicator_code, str) or not indicator_code:
        raise MacroAuthorityReleaseError("Macro release missing indicator_code")
    observation_period = payload.get("observation_period")
    if not isinstance(observation_period, str) or not observation_period:
        raise MacroAuthorityReleaseError("Macro release missing observation_period")
    publication_date = payload.get("publication_date")
    if not isinstance(publication_date, str) or not publication_date:
        raise MacroAuthorityReleaseError("Macro release missing publication_date")
    revision_version = payload.get("revision_version")
    try:
        revision = int(str(revision_version))
    except (TypeError, ValueError) as exc:
        raise MacroAuthorityReleaseError("Macro release missing revision_version") from exc
    if revision < 1:
        raise MacroAuthorityReleaseError("Macro revision_version must be positive")
    available_to_system_at = payload.get("available_to_system_at")
    if not isinstance(available_to_system_at, str) or not available_to_system_at:
        raise MacroAuthorityReleaseError("Macro release missing available_to_system_at")
    try:
        published = date.fromisoformat(publication_date)
        available_raw = datetime.fromisoformat(
            available_to_system_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise MacroAuthorityReleaseError("Macro release timeline is invalid") from exc
    if available_raw.tzinfo is None or available_raw.utcoffset() is None:
        raise MacroAuthorityReleaseError("Macro availability must be timezone-aware")
    if available_raw.date() < published:
        raise MacroAuthorityReleaseError(
            "Macro release cannot be available before publication_date"
        )
    _observation_period_end(observation_period)
    request = payload.get("_astock_request")
    if (
        not isinstance(request, dict)
        or request.get("purpose") != "MACRO_ECONOMIC_RELEASE"
        or request.get("indicator_code") != indicator_code
    ):
        raise MacroAuthorityReleaseError("Macro release request provenance is invalid")
    source_url = payload.get("source_url")
    parsed = urlparse(str(source_url or ""))
    expected_host = _ALLOWED_MACRO_HOSTS.get(provider_id)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or expected_host is None
        or not (host == expected_host or host.endswith(f".{expected_host}"))
    ):
        raise MacroAuthorityReleaseError("Macro release source_url is not official HTTPS")
    data_points = payload.get("data_points")
    if not isinstance(data_points, list) or not data_points:
        raise MacroAuthorityReleaseError("Macro release missing or empty data_points")
    for point in data_points:
        if (
            not isinstance(point, dict)
            or not isinstance(point.get("period"), str)
            or not point.get("period")
            or ("value" not in point and "price" not in point)
            or not isinstance(point.get("unit"), str)
            or not point.get("unit")
        ):
            raise MacroAuthorityReleaseError("Macro release contains malformed data_points")
    history = payload.get("revision_history")
    if not isinstance(history, list) or not history:
        raise MacroAuthorityReleaseError("Macro release missing revision_history")
    versions: list[int] = []
    history_dates: list[date] = []
    for item in history:
        if not isinstance(item, dict):
            raise MacroAuthorityReleaseError("Macro revision_history is malformed")
        try:
            versions.append(int(str(item["version"])))
            history_dates.append(date.fromisoformat(str(item["date"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise MacroAuthorityReleaseError("Macro revision_history is malformed") from exc
    if (
        versions != list(range(1, revision + 1))
        or history_dates != sorted(history_dates)
        or history_dates[-1] > available_raw.date()
    ):
        raise MacroAuthorityReleaseError(
            "Macro revision_history must be contiguous, ordered, current, and observed"
        )


__all__ = [
    "MacroAuthorityReleaseError",
    "NbsStatisticalReleaseProvider",
    "PbocMonetaryPolicyReleaseProvider",
    "MofFiscalPolicyReleaseProvider",
    "NdrcPricingPolicyReleaseProvider",
]
