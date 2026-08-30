"""Persistent provider+capability circuit breaking for external acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from pathlib import Path

import httpx
import yaml

from astock.core.errors import AStockError, FailureClass
from astock.core.state import StateStore


class SourceFailureClass(StrEnum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    RATE_LIMITED = "RATE_LIMITED"
    REMOTE_5XX = "REMOTE_5XX"
    AUTH_CONFIG = "AUTH_CONFIG"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    COVERAGE_INCOMPLETE = "COVERAGE_INCOMPLETE"
    CONFLICTED = "CONFLICTED"
    LOCAL_CORRUPTION = "LOCAL_CORRUPTION"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


def scoped_source_capability(capability: str, scope: str | None = None) -> str:
    """Return a durable breaker key without coupling independent market scopes."""

    normalized_capability = capability.strip()
    normalized_scope = scope.strip() if scope is not None else ""
    if not normalized_capability:
        raise ValueError("source resilience capability must be non-empty")
    return (
        normalized_capability
        if not normalized_scope
        else f"{normalized_capability}@{normalized_scope}"
    )


@dataclass(frozen=True, slots=True)
class SourceResiliencePolicy:
    schema_version: str
    default_elapsed_budget_seconds: int
    failure_threshold: int
    cooldown_seconds: int
    half_open_max_probes: int
    retry_after_cap_seconds: int
    immediate_open_failure_classes: frozenset[SourceFailureClass]
    counted_failure_classes: frozenset[SourceFailureClass]


def load_source_resilience_policy(path: Path) -> SourceResiliencePolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid source resilience policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "source-resilience-v1":
        raise ValueError("Unsupported source resilience policy")
    breaker = raw.get("breaker")
    if not isinstance(breaker, dict):
        raise ValueError("Source resilience breaker policy is missing")
    failure_threshold = int(breaker.get("failure_threshold") or 0)
    cooldown_seconds = int(breaker.get("cooldown_seconds") or 0)
    half_open_max_probes = int(breaker.get("half_open_max_probes") or 0)
    retry_after_cap_seconds = int(breaker.get("retry_after_cap_seconds") or 0)
    default_elapsed_budget_seconds = int(raw.get("default_elapsed_budget_seconds") or 0)
    if (
        min(
            failure_threshold,
            cooldown_seconds,
            half_open_max_probes,
            retry_after_cap_seconds,
            default_elapsed_budget_seconds,
        )
        < 1
    ):
        raise ValueError("Source resilience numeric policy values must be positive")
    if half_open_max_probes != 1:
        raise ValueError("source-resilience-v1 supports exactly one half-open probe per capability")
    immediate = frozenset(
        SourceFailureClass(str(item)) for item in breaker.get("immediate_open_failure_classes", [])
    )
    counted = frozenset(
        SourceFailureClass(str(item)) for item in breaker.get("counted_failure_classes", [])
    )
    if not counted or not immediate.issubset(counted):
        raise ValueError("Source resilience failure classes are inconsistent")
    return SourceResiliencePolicy(
        schema_version="source-resilience-v1",
        default_elapsed_budget_seconds=default_elapsed_budget_seconds,
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown_seconds,
        half_open_max_probes=half_open_max_probes,
        retry_after_cap_seconds=retry_after_cap_seconds,
        immediate_open_failure_classes=immediate,
        counted_failure_classes=counted,
    )


class SourceCircuitBreaker:
    """Durable circuit breaker keyed by logical source and capability."""

    def __init__(
        self,
        state: StateStore,
        policy: SourceResiliencePolicy | None = None,
    ) -> None:
        self.state = state
        root = Path(__file__).resolve().parents[3]
        self.policy = policy or load_source_resilience_policy(
            root / "configs" / "source_resilience.yaml"
        )

    def is_available(
        self,
        source_id: str,
        capability: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        now = _aware(at)
        row = self._row(source_id, capability)
        if row is None:
            return True
        try:
            state = CircuitState(str(row["state"]))
        except (TypeError, ValueError):
            return False
        if state is CircuitState.CLOSED:
            return True
        try:
            retry_after_at = _parse_optional_datetime(row["retry_after_at"])
        except ValueError:
            return False
        if state is CircuitState.OPEN:
            return retry_after_at is not None and retry_after_at <= now
        if not bool(row["half_open_probe_in_flight"]):
            return True
        try:
            updated_at = _parse_optional_datetime(row["updated_at"])
        except ValueError:
            return False
        return updated_at is not None and (
            updated_at + timedelta(seconds=self.policy.cooldown_seconds) <= now
        )

    def claim_attempt(
        self,
        source_id: str,
        capability: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        now = _aware(at)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_circuit_breaker WHERE source_id=? AND capability=?",
                (source_id, capability),
            ).fetchone()
            if row is None:
                return True
            try:
                state = CircuitState(str(row["state"]))
                retry_after_at = _parse_optional_datetime(row["retry_after_at"])
            except (TypeError, ValueError):
                return False
            if state is CircuitState.CLOSED:
                return True
            if state is CircuitState.OPEN:
                if retry_after_at is None or retry_after_at > now:
                    return False
                if self.policy.half_open_max_probes < 1:
                    return False
                connection.execute(
                    "UPDATE source_circuit_breaker SET state=?,half_open_probe_in_flight=1,"
                    "updated_at=? WHERE source_id=? AND capability=?",
                    (CircuitState.HALF_OPEN.value, now.isoformat(), source_id, capability),
                )
                return True
            if bool(row["half_open_probe_in_flight"]):
                try:
                    updated_at = _parse_optional_datetime(row["updated_at"])
                except ValueError:
                    return False
                if updated_at is None or (
                    updated_at + timedelta(seconds=self.policy.cooldown_seconds) > now
                ):
                    return False
                connection.execute(
                    "UPDATE source_circuit_breaker SET updated_at=? "
                    "WHERE source_id=? AND capability=?",
                    (now.isoformat(), source_id, capability),
                )
                return True
            connection.execute(
                "UPDATE source_circuit_breaker SET half_open_probe_in_flight=1,updated_at=? "
                "WHERE source_id=? AND capability=?",
                (now.isoformat(), source_id, capability),
            )
            return True

    def record_success(
        self,
        source_id: str,
        capability: str,
        *,
        at: datetime | None = None,
    ) -> None:
        now = _aware(at)
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO source_circuit_breaker(source_id,capability,state,failure_count,"
                "opened_at,retry_after_at,last_failure_class,half_open_probe_in_flight,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,capability) DO UPDATE SET "
                "state=excluded.state,failure_count=0,opened_at=NULL,retry_after_at=NULL,"
                "last_failure_class=NULL,half_open_probe_in_flight=0,updated_at=excluded.updated_at",
                (
                    source_id,
                    capability,
                    CircuitState.CLOSED.value,
                    0,
                    None,
                    None,
                    None,
                    0,
                    now.isoformat(),
                ),
            )

    def record_failure(
        self,
        source_id: str,
        capability: str,
        failure_class: SourceFailureClass,
        *,
        retry_after_seconds: int | None = None,
        at: datetime | None = None,
    ) -> CircuitState:
        now = _aware(at)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM source_circuit_breaker WHERE source_id=? AND capability=?",
                (source_id, capability),
            ).fetchone()
            previous_count = int(row["failure_count"]) if row is not None else 0
            previous_state = (
                CircuitState(str(row["state"])) if row is not None else CircuitState.CLOSED
            )
            counted = failure_class in self.policy.counted_failure_classes
            failure_count = previous_count + (1 if counted else 0)
            immediate = failure_class in self.policy.immediate_open_failure_classes
            should_open = (
                immediate
                or previous_state is CircuitState.HALF_OPEN
                or failure_count >= self.policy.failure_threshold
            )
            if should_open:
                state = CircuitState.OPEN
                requested_delay = retry_after_seconds or self.policy.cooldown_seconds
                delay = min(
                    max(requested_delay, self.policy.cooldown_seconds),
                    self.policy.retry_after_cap_seconds,
                )
                opened_at = now
                retry_after_at = now + timedelta(seconds=delay)
            else:
                state = CircuitState.CLOSED
                opened_at = None
                retry_after_at = None
            connection.execute(
                "INSERT INTO source_circuit_breaker(source_id,capability,state,failure_count,"
                "opened_at,retry_after_at,last_failure_class,half_open_probe_in_flight,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,capability) DO UPDATE SET "
                "state=excluded.state,failure_count=excluded.failure_count,opened_at=excluded.opened_at,"
                "retry_after_at=excluded.retry_after_at,last_failure_class=excluded.last_failure_class,"
                "half_open_probe_in_flight=0,updated_at=excluded.updated_at",
                (
                    source_id,
                    capability,
                    state.value,
                    failure_count,
                    opened_at.isoformat() if opened_at else None,
                    retry_after_at.isoformat() if retry_after_at else None,
                    failure_class.value,
                    0,
                    now.isoformat(),
                ),
            )
            return state

    def reset(self, source_id: str, capability: str) -> None:
        with self.state.transaction() as connection:
            connection.execute(
                "DELETE FROM source_circuit_breaker WHERE source_id=? AND capability=?",
                (source_id, capability),
            )

    def status(self, source_id: str, capability: str) -> dict[str, object]:
        row = self._row(source_id, capability)
        if row is None:
            return {
                "source_id": source_id,
                "capability": capability,
                "state": CircuitState.CLOSED.value,
                "failure_count": 0,
                "retry_after_at": None,
                "last_failure_class": None,
            }
        return {
            "source_id": source_id,
            "capability": capability,
            "state": str(row["state"]),
            "failure_count": int(row["failure_count"]),
            "retry_after_at": row["retry_after_at"],
            "last_failure_class": row["last_failure_class"],
        }

    def _row(self, source_id: str, capability: str):
        with self.state.connect() as connection:
            return connection.execute(
                "SELECT * FROM source_circuit_breaker WHERE source_id=? AND capability=?",
                (source_id, capability),
            ).fetchone()


def parse_retry_after_seconds(
    value: str | None,
    *,
    now: datetime | None = None,
) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdecimal():
        return max(0, int(stripped))
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = int((parsed.astimezone(UTC) - _aware(now)).total_seconds())
    return max(0, seconds)


def classify_http_status(status_code: int) -> SourceFailureClass | None:
    if status_code == 429:
        return SourceFailureClass.RATE_LIMITED
    if status_code in {401, 403}:
        return SourceFailureClass.AUTH_CONFIG
    if status_code in {502, 503, 504}:
        return SourceFailureClass.REMOTE_5XX
    return None


def classify_source_error(error: BaseException) -> SourceFailureClass:
    if isinstance(error, httpx.TimeoutException):
        return SourceFailureClass.TRANSIENT_NETWORK
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code == 429:
            return SourceFailureClass.RATE_LIMITED
        if status_code in {401, 403}:
            return SourceFailureClass.AUTH_CONFIG
        if status_code >= 500:
            return SourceFailureClass.REMOTE_5XX
        return SourceFailureClass.INVALID_PAYLOAD
    if isinstance(error, httpx.TransportError):
        return SourceFailureClass.TRANSIENT_NETWORK
    if isinstance(error, AStockError):
        mapping = {
            FailureClass.NETWORK: SourceFailureClass.TRANSIENT_NETWORK,
            FailureClass.TIMEOUT: SourceFailureClass.TRANSIENT_NETWORK,
            FailureClass.RATE_LIMITED: SourceFailureClass.RATE_LIMITED,
            FailureClass.AUTH_REQUIRED: SourceFailureClass.AUTH_CONFIG,
            FailureClass.ACCESS_RESTRICTED: SourceFailureClass.AUTH_CONFIG,
            FailureClass.INVALID_RESPONSE: SourceFailureClass.INVALID_PAYLOAD,
            FailureClass.PAGINATION_CYCLE: SourceFailureClass.COVERAGE_INCOMPLETE,
            FailureClass.DATA_QUALITY: SourceFailureClass.COVERAGE_INCOMPLETE,
            FailureClass.CAPABILITY_UNAVAILABLE: SourceFailureClass.COVERAGE_INCOMPLETE,
            FailureClass.CONFLICT: SourceFailureClass.CONFLICTED,
            FailureClass.STORAGE: SourceFailureClass.LOCAL_CORRUPTION,
        }
        return mapping.get(error.failure_class, SourceFailureClass.INVALID_PAYLOAD)
    failure_code = getattr(error, "failure_code", None)
    if isinstance(failure_code, str):
        normalized = failure_code.upper()
        if "TIMEOUT" in normalized or "NETWORK" in normalized or "HTTP_FAILED" in normalized:
            return SourceFailureClass.TRANSIENT_NETWORK
        if "429" in normalized or "RATE_LIMIT" in normalized:
            return SourceFailureClass.RATE_LIMITED
        if any(token in normalized for token in ("AUTH", "ACCESS", "401", "403")):
            return SourceFailureClass.AUTH_CONFIG
        if "DIALECT" in normalized or "SCHEMA" in normalized or "NORMALIZATION" in normalized:
            return SourceFailureClass.SCHEMA_DRIFT
        if "PAGINATION" in normalized or "INCOMPLETE" in normalized or "COVERAGE" in normalized:
            return SourceFailureClass.COVERAGE_INCOMPLETE
        if "CONFLICT" in normalized:
            return SourceFailureClass.CONFLICTED
        if "RAW_INVALID" in normalized or "INVALID" in normalized:
            return SourceFailureClass.INVALID_PAYLOAD
    if isinstance(error, OSError):
        return SourceFailureClass.TRANSIENT_NETWORK
    return SourceFailureClass.INVALID_PAYLOAD


def _aware(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("source resilience timestamps must be timezone-aware")
    return current.astimezone(UTC)


def _parse_optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "CircuitState",
    "SourceCircuitBreaker",
    "SourceFailureClass",
    "SourceResiliencePolicy",
    "classify_http_status",
    "classify_source_error",
    "load_source_resilience_policy",
    "parse_retry_after_seconds",
]
