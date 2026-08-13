"""Provider protocol and shared HTTP response persistence."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol

import httpx

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.runtime import build_provider_http_client
from astock.schemas import (
    BarRequest,
    DataProviderCapability,
    MarketDataBatch,
    SourceSnapshot,
)


class MarketDataProvider(Protocol):
    provider_id: str

    def capability(self) -> DataProviderCapability: ...

    def fetch_bars(self, request: BarRequest) -> MarketDataBatch: ...


class HttpProviderBase:
    provider_id: str

    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.object_store = object_store
        self.state = state
        self.client = client or build_provider_http_client(self.provider_id)

    def _get(self, url: str, *, params: dict[str, str | int]) -> tuple[httpx.Response, int]:
        started = time.perf_counter()
        try:
            response = self.client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.provider_id} timed out",
                failure_class=FailureClass.TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"{self.provider_id} network request failed",
                failure_class=FailureClass.NETWORK,
                retryable=True,
                details={"error": str(exc)},
            ) from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code == 429:
            raise ProviderError(
                f"{self.provider_id} rate limited the request",
                failure_class=FailureClass.RATE_LIMITED,
                retryable=False,
                details={"status_code": 429},
            )
        if response.status_code in {401, 403}:
            raise ProviderError(
                f"{self.provider_id} denied access",
                failure_class=FailureClass.ACCESS_RESTRICTED,
                retryable=False,
                details={"status_code": response.status_code},
            )
        if response.status_code >= 500:
            raise ProviderError(
                f"{self.provider_id} server error {response.status_code}",
                failure_class=FailureClass.NETWORK,
                retryable=True,
                details={"status_code": response.status_code},
            )
        if response.status_code >= 400:
            raise ProviderError(
                f"{self.provider_id} HTTP error {response.status_code}",
                failure_class=FailureClass.INVALID_RESPONSE,
                retryable=False,
                details={"status_code": response.status_code},
            )
        return response, latency_ms

    def _persist_response(self, response: httpx.Response) -> SourceSnapshot:
        now = datetime.now(UTC)
        object_ref = self.object_store.put_bytes(response.content)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:{object_ref.sha256}",
            source_id=self.provider_id,
            object_sha256=object_ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=str(response.request.url),
            mime=response.headers.get("content-type", "application/octet-stream").split(";")[0],
            byte_size=object_ref.byte_size,
            headers_hash=content_hash(sorted(response.headers.items())),
        )
        self.state.register_snapshot(snapshot)
        return snapshot
