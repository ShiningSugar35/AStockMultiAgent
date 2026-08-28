"""Bounded provider HTTP transport resilience without mutating global proxy state."""

from __future__ import annotations

import random
import time
from collections.abc import Mapping
from typing import Protocol

import httpx

from astock.core.source_resilience import classify_http_status, parse_retry_after_seconds


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
        elapsed_budget_seconds: float = 30.0,
    ) -> None:
        if not lane_trust_env:
            raise ValueError("HTTP resilience requires at least one transport lane")
        if max_attempts < 1:
            raise ValueError("HTTP resilience max_attempts must be positive")
        if elapsed_budget_seconds <= 0:
            raise ValueError("HTTP resilience elapsed budget must be positive")
        if timeout_seconds > elapsed_budget_seconds:
            raise ValueError("HTTP timeout cannot exceed the acquisition elapsed budget")
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.elapsed_budget_seconds = elapsed_budget_seconds
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
        started = time.monotonic()
        for attempt in range(attempt_limit):
            remaining = self._remaining_budget(started)
            if remaining <= 0:
                if last_error is not None:
                    raise last_error
                if last_response is not None:
                    return last_response
                raise httpx.TimeoutException(
                    "HTTP acquisition elapsed budget exhausted",
                    request=httpx.Request(normalized_method, url),
                )
            lane_name, client = self._lanes[attempt % len(self._lanes)]
            try:
                response = client.request(
                    normalized_method,
                    url,
                    params=params,
                    data=data,
                    timeout=min(self.timeout_seconds, remaining),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 >= attempt_limit or not self._sleep(attempt, started):
                    raise
                continue
            response.extensions["astock_transport_lane"] = lane_name
            response.extensions["astock_transport_attempt"] = attempt + 1
            response.extensions["astock_elapsed_budget_seconds"] = self.elapsed_budget_seconds
            failure_class = classify_http_status(response.status_code)
            if failure_class is not None:
                response.extensions["astock_failure_class"] = failure_class.value
            if response.status_code == 429:
                retry_after = parse_retry_after_seconds(response.headers.get("Retry-After"))
                if retry_after is not None:
                    response.extensions["astock_retry_after_seconds"] = retry_after
            if response.status_code not in self.retry_status_codes:
                return response
            last_response = response
            if attempt + 1 >= attempt_limit:
                return response
            if not self._sleep(attempt, started):
                return response
            response.close()
        if last_error is not None:  # pragma: no cover - defensive boundary
            raise last_error
        assert last_response is not None
        return last_response

    def close(self) -> None:
        for _, client in self._lanes:
            client.close()

    def _sleep(self, attempt: int, started: float) -> bool:
        delay = self.backoff_seconds * (2**attempt)
        if self.jitter_seconds > 0:
            delay += random.uniform(0.0, self.jitter_seconds)
        remaining = self._remaining_budget(started)
        if remaining <= 0 or delay >= remaining:
            return False
        if delay > 0:
            time.sleep(delay)
        return self._remaining_budget(started) > 0

    def _remaining_budget(self, started: float) -> float:
        return self.elapsed_budget_seconds - (time.monotonic() - started)


__all__ = ["HttpClientLike", "ResilientHttpClient"]
