"""Recorded-first macro-economic authority data providers.

Each provider fetches a specific indicator from a designated official website,
freezes the raw response, and exposes structured release metadata including
observation period, publication date, revision version, and system availability
time.  The adapters deliberately use the same recorded-first pattern as the
BSE official reference: live mode fetches from the official website, recorded
mode reads from a fixture file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from astock.providers.base import HttpProviderBase
from astock.providers.http_resilience import HttpClientLike


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
        content = path.read_bytes()
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MacroAuthorityReleaseError("Recorded NBS fixture is not JSON") from exc
        if not isinstance(payload, dict):
            raise MacroAuthorityReleaseError("Recorded NBS fixture root must be an object")
        _validate_macro_release(payload, "nbs-statistical-release")
        response = httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", f"recorded://{path.as_posix()}"),
        )
        snapshot = self._persist_response(response)
        return payload, snapshot


class PbocMonetaryPolicyReleaseProvider(HttpProviderBase):
    """People's Bank of China monetary policy indicator provider.

    Fetches monetary policy data from the PBOC official website (pbc.gov.cn).
    Each indicator has a fixed publication schedule and may be revised.
    """

    provider_id = "pboc-monetary-policy-release"
    base_url = "http://www.pbc.gov.cn"

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
        content = path.read_bytes()
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MacroAuthorityReleaseError("Recorded PBOC fixture is not JSON") from exc
        if not isinstance(payload, dict):
            raise MacroAuthorityReleaseError("Recorded PBOC fixture root must be an object")
        _validate_macro_release(payload, "pboc-monetary-policy-release")
        response = httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", f"recorded://{path.as_posix()}"),
        )
        snapshot = self._persist_response(response)
        return payload, snapshot


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
        content = path.read_bytes()
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MacroAuthorityReleaseError("Recorded MOF fixture is not JSON") from exc
        if not isinstance(payload, dict):
            raise MacroAuthorityReleaseError("Recorded MOF fixture root must be an object")
        _validate_macro_release(payload, "mof-fiscal-policy-release")
        response = httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", f"recorded://{path.as_posix()}"),
        )
        snapshot = self._persist_response(response)
        return payload, snapshot


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
        content = path.read_bytes()
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MacroAuthorityReleaseError("Recorded NDRC fixture is not JSON") from exc
        if not isinstance(payload, dict):
            raise MacroAuthorityReleaseError("Recorded NDRC fixture root must be an object")
        _validate_macro_release(payload, "ndrc-pricing-policy-release")
        response = httpx.Response(
            200,
            content=content,
            headers={"content-type": "application/json; charset=utf-8"},
            request=httpx.Request("GET", f"recorded://{path.as_posix()}"),
        )
        snapshot = self._persist_response(response)
        return payload, snapshot


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
    if not isinstance(revision_version, (int, str)):
        raise MacroAuthorityReleaseError("Macro release missing revision_version")
    available_to_system_at = payload.get("available_to_system_at")
    if not isinstance(available_to_system_at, str) or not available_to_system_at:
        raise MacroAuthorityReleaseError("Macro release missing available_to_system_at")
    data_points = payload.get("data_points")
    if not isinstance(data_points, list) or not data_points:
        raise MacroAuthorityReleaseError("Macro release missing or empty data_points")


__all__ = [
    "MacroAuthorityReleaseError",
    "NbsStatisticalReleaseProvider",
    "PbocMonetaryPolicyReleaseProvider",
    "MofFiscalPolicyReleaseProvider",
    "NdrcPricingPolicyReleaseProvider",
]
