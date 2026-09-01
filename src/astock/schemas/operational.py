"""Operational observability contracts; never investment or ledger authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from astock.schemas.base import AStockModel


class OperationalEventKind(StrEnum):
    OPERATIONAL = "OPERATIONAL"
    AUDIT = "AUDIT"
    SECURITY = "SECURITY"


class OperationalSeverity(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class StructuredLogEvent(AStockModel):
    """One bounded, correlation-addressable internal event."""

    schema_version: str = "structured-log-event-v1"
    correlation_id: str = Field(min_length=8, max_length=200)
    event_kind: OperationalEventKind = OperationalEventKind.OPERATIONAL
    component: str = Field(min_length=1, max_length=200)
    event: str = Field(min_length=1, max_length=200)
    severity: OperationalSeverity = OperationalSeverity.INFO
    failure_class: str | None = Field(default=None, max_length=160)
    run_id: str | None = Field(default=None, max_length=240)
    request_id: str | None = Field(default=None, max_length=240)
    context: dict[str, Any] = Field(default_factory=dict)


class PublicErrorSummary(AStockModel):
    """Bounded user impact plus a separate developer summary keyed by correlation id."""

    schema_version: str = "public-error-summary-v1"
    correlation_id: str = Field(min_length=8, max_length=200)
    failure_class: str = Field(min_length=1, max_length=160)
    investor_message: str = Field(min_length=1, max_length=500)
    developer_summary: str = Field(min_length=1, max_length=1000)
    safe_to_send: Literal[True] = True
    raw_error_exposed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False


__all__ = [
    "OperationalEventKind",
    "OperationalSeverity",
    "PublicErrorSummary",
    "StructuredLogEvent",
]
