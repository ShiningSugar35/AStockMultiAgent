"""Bounded provider HTTP transport resilience without mutating global proxy state."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Protocol

import httpx


class HttpClientLike(Protocol):
    @property
    def headers(self) -> httpx.Headers: ...

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response: ...

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> httpx.Response: ...

    def close(self) -> None: ...


class ResilientHttpClient:
    """Try a bounded sequence of ENV/DIRECT HTTP lanes with jittered backoff."""

    def __init__(
        self,
        *,
        timeout_seconds: float,
        follow_redirects: bool,
        headers: dict[str, str],
        lane_trust_env: tuple[bool, ...],
        max_attempts: int,
        backoff_seconds: float,
        jitter_seconds: float,
        retry_status_codes: tuple[int, ...],
        retry_methods: tuple[str, ...] = ("GET", "HEAD"),
    ) -> None:
        if not lane_trust_env:
            raise ValueError("HTTP resilience requires at least one transport lane")
        if max_attempts < 1:
            raise ValueError("HTTP resilience max_attempts must be positive")
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.jitter_seconds = jitter_seconds
        self.retry_status_codes = frozenset(retry_status_codes)
        self.retry_methods = frozenset(item.upper() for item in retry_methods)
        self._headers = httpx.Headers(headers)
        self._lanes = [
            (
                "ENV" if trust_env else "DIRECT",
                httpx.Client(
                    timeout=timeout_seconds,
                    follow_redirects=follow_redirects,
                    trust_env=trust_env,
                    headers=headers,
                ),
            )
            for trust_env in lane_trust_env
        ]

    @property
    def headers(self) -> httpx.Headers:
        return self._headers

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        return self.request("GET", url, params=params)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str | int] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        normalized_method = method.upper()
        attempt_limit = self.max_attempts if normalized_method in self.retry_methods else 1
        last_error: httpx.HTTPError | None = None
        last_response: httpx.Response | None = None
        for attempt in range(attempt_limit):
            lane_name, client = self._lanes[attempt % len(self._lanes)]
            try:
                response = client.request(normalized_method, url, params=params, data=data)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= attempt_limit:
                    raise
                self._sleep(attempt)
                continue
            response.extensions["astock_transport_lane"] = lane_name
            response.extensions["astock_transport_attempt"] = attempt + 1
            if response.status_code not in self.retry_status_codes:
                return response
            last_response = response
            if attempt + 1 >= attempt_limit:
                return response
            response.close()
            self._sleep(attempt)
        if last_error is not None:  # pragma: no cover - defensive boundary
            raise last_error
        assert last_response is not None
        return last_response

    def close(self) -> None:
        for _, client in self._lanes:
            client.close()

    def _sleep(self, attempt: int) -> None:
        delay = self.backoff_seconds * (2**attempt)
        if self.jitter_seconds > 0:
            delay += random.uniform(0.0, self.jitter_seconds)
        if delay > 0:
            time.sleep(delay)


__all__ = ["HttpClientLike", "ResilientHttpClient"]
