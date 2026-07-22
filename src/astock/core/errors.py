"""Stable error taxonomy used by providers, jobs, and CLI commands."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class FailureClass(StrEnum):
    NETWORK = "NETWORK"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    PAGINATION_CYCLE = "PAGINATION_CYCLE"
    DATA_QUALITY = "DATA_QUALITY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    CONFLICT = "CONFLICT"
    POLICY_REJECTED = "POLICY_REJECTED"
    STORAGE = "STORAGE"
    INTERNAL = "INTERNAL"


class AStockError(Exception):
    """Base error with a stable machine-readable classification."""

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass = FailureClass.INTERNAL,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = retryable
        self.details = details or {}


class ProviderError(AStockError):
    """External provider request or contract error."""


class DataQualityError(AStockError):
    """Data failed a hard quality gate."""


class PolicyError(AStockError):
    """A requested write violates a project policy."""


class StorageError(AStockError):
    """Durable state could not be read or written safely."""
