"""Structured, redacted operational logging with bounded local retention."""

from __future__ import annotations

import atexit
import copy
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

import yaml

from astock.schemas.operational import (
    OperationalEventKind,
    OperationalSeverity,
    StructuredLogEvent,
)

_LOGGER_NAME = "astock"
_CORRELATION_ID: ContextVar[str | None] = ContextVar("astock_correlation_id", default=None)
_RUN_ID: ContextVar[str | None] = ContextVar("astock_run_id", default=None)
_REQUEST_ID: ContextVar[str | None] = ContextVar("astock_request_id", default=None)
_LISTENER: logging.handlers.QueueListener | None = None
_EVENT_QUEUE: queue.Queue[logging.LogRecord] | None = None
_QUEUE_HANDLER: logging.Handler | None = None
_SINKS: list[logging.Handler] = []
_CONFIGURED_KEY: tuple[object, ...] | None = None
_CONFIG_LOCK = Lock()

_DEFAULT_MAX_EVENT_BYTES = 65536
_DEFAULT_MAX_MESSAGE_CHARS = 4096
_DEFAULT_MAX_CONTEXT_ITEMS = 100
_DEFAULT_MAX_CONTEXT_DEPTH = 8

_SECRET_KEY_RE = re.compile(
    r"(?i)(authorization|proxy-authorization|cookie|set-cookie|password|passwd|pwd|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|secret|client[_-]?secret|session)"
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)\b((?:proxy-)?authorization|(?:set-)?cookie)\s*[:=]\s*[^\r\n]*"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|password|"
    r"passwd|pwd|client[_-]?secret|secret|token|session)\b\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&\r\n]+)"
)
_URL_USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_PRIVATE_PATH_RES = (
    re.compile(
        r"(?i)\b[A-Z]:\\Users\\[^\\/:*?\"<>|\r\n]+"
        r"(?:\\[^\\/:*?\"<>|\r\n]+)*"
    ),
    re.compile(
        r"(?i)(?<![\w/])/(?:home|Users)/[^/\s,;]+(?:/[^\s,;]+)*"
    ),
)
_REQUEST_BODY_KEYS = {
    "request_body",
    "response_body",
    "raw_body",
    "payload_body",
    "body",
    "request_json",
    "response_json",
    "request_data",
    "response_data",
    "form_data",
    "multipart",
    "files",
}


@dataclass(frozen=True, slots=True)
class OperationalContext:
    correlation_id: str
    run_id: str | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class LoggingPolicy:
    schema_version: str
    level: int
    file_name: str
    max_bytes: int
    backup_count: int
    retention_days: int
    queue_size: int
    max_event_bytes: int
    max_message_chars: int
    max_context_items: int
    max_context_depth: int
    redact_private_paths: bool
    redact_request_bodies: bool
    redacted_keys: frozenset[str]

    @property
    def maximum_file_bytes(self) -> int:
        """Hard bound for the active log plus regular and quarantined siblings."""

        return self.max_bytes * (self.backup_count + 1)


@dataclass(frozen=True, slots=True)
class LoggingConfiguration:
    policy: LoggingPolicy | None
    file_sink_enabled: bool
    log_file: Path | None


def load_logging_policy(path: Path) -> LoggingPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid operational logging policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "operational-logging-policy-v1":
        raise ValueError("Unsupported operational logging policy")

    level_name = str(raw.get("level") or "INFO").upper()
    level = logging.getLevelNamesMapping().get(level_name)
    if not isinstance(level, int):
        raise ValueError("Invalid operational logging level")

    file_name = str(raw.get("file_name") or "").strip()
    if not file_name or Path(file_name).name != file_name:
        raise ValueError("Operational log file_name must be a simple filename")

    max_bytes = int(raw.get("max_bytes") or 0)
    backup_count = int(raw.get("backup_count") or 0)
    retention_days = int(raw.get("retention_days") or 0)
    queue_size = int(raw.get("queue_size") or 0)
    max_event_bytes = int(raw.get("max_event_bytes") or _DEFAULT_MAX_EVENT_BYTES)
    max_message_chars = int(raw.get("max_message_chars") or _DEFAULT_MAX_MESSAGE_CHARS)
    max_context_items = int(raw.get("max_context_items") or _DEFAULT_MAX_CONTEXT_ITEMS)
    max_context_depth = int(raw.get("max_context_depth") or _DEFAULT_MAX_CONTEXT_DEPTH)
    if max_bytes < 1024 or backup_count < 1 or retention_days < 1 or queue_size < 64:
        raise ValueError("Operational logging bounds must be positive and non-trivial")
    if not 512 <= max_event_bytes <= max_bytes:
        raise ValueError("Operational max_event_bytes must be in 512..max_bytes")
    if max_message_chars < 128 or max_context_items < 8 or max_context_depth < 2:
        raise ValueError("Operational event bounds are too small")

    raw_keys = raw.get("redacted_keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("Operational logging redacted_keys must be non-empty")
    return LoggingPolicy(
        schema_version="operational-logging-policy-v1",
        level=level,
        file_name=file_name,
        max_bytes=max_bytes,
        backup_count=backup_count,
        retention_days=retention_days,
        queue_size=queue_size,
        max_event_bytes=max_event_bytes,
        max_message_chars=max_message_chars,
        max_context_items=max_context_items,
        max_context_depth=max_context_depth,
        redact_private_paths=bool(raw.get("redact_private_paths", True)),
        redact_request_bodies=bool(raw.get("redact_request_bodies", True)),
        redacted_keys=frozenset(
            str(item).strip().casefold()
            for item in raw_keys
            if str(item).strip()
        ),
    )


class JsonFormatter(logging.Formatter):
    """Render one bounded JSON line after defense-in-depth redaction."""

    def __init__(self, policy: LoggingPolicy | None = None) -> None:
        super().__init__()
        self.policy = policy

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _bounded_text(record.getMessage(), self.policy),
        }
        event = getattr(record, "structured_event", None)
        if isinstance(event, Mapping):
            sanitized_event = sanitize_for_log(dict(event), policy=self.policy)
            payload["event"] = sanitized_event
            if isinstance(sanitized_event, Mapping):
                for key in ("correlation_id", "run_id", "request_id"):
                    value = sanitized_event.get(key)
                    if value:
                        payload[key] = value
        if record.exc_info:
            payload["exception"] = _bounded_text(
                redact_text(
                    self.formatException(record.exc_info),
                    redact_private_paths=(
                        self.policy is None or self.policy.redact_private_paths
                    ),
                ),
                self.policy,
            )
        return _bounded_json(payload, self.policy)


class DailySizeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate on date or size and enforce a directory-level byte bound."""

    def __init__(self, filename: Path, *, policy: LoggingPolicy) -> None:
        super().__init__(
            filename,
            mode="a",
            maxBytes=policy.max_bytes,
            backupCount=policy.backup_count,
            encoding="utf-8",
            delay=True,
        )
        self._active_date = date.today()
        self._policy = policy
        self._log_path = Path(filename)

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        return bool(date.today() != self._active_date or super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802
        super().doRollover()
        self._active_date = date.today()
        _prune_expired_logs(self._log_path, retention_days=self._policy.retention_days)
        _enforce_log_budget(self._log_path, max_total_bytes=self._policy.maximum_file_bytes)

    def close(self) -> None:
        try:
            super().close()
        finally:
            _enforce_log_budget(
                self._log_path,
                max_total_bytes=self._policy.maximum_file_bytes,
            )


class BoundedQueueHandler(logging.handlers.QueueHandler):
    """Never block a research path because observability is saturated."""

    def __init__(
        self,
        event_queue: queue.Queue[logging.LogRecord],
        fallback: logging.Handler,
    ) -> None:
        super().__init__(event_queue)
        self.fallback = fallback

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Keep exception and structured-event data for the in-process listener."""

        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            overflow = logging.LogRecord(
                name="astock.logging",
                level=logging.WARNING,
                pathname=__file__,
                lineno=0,
                msg="operational log queue full; event dropped",
                args=(),
                exc_info=None,
            )
            self.fallback.handle(overflow)


def configure_logging(
    level: int | None = None,
    *,
    log_dir: Path | None = None,
    policy_path: Path | None = None,
    create_log_dir: bool = True,
) -> LoggingConfiguration:
    """Configure the ``astock`` logger with safe stderr and an optional file sink."""

    global _CONFIGURED_KEY, _EVENT_QUEUE, _LISTENER, _QUEUE_HANDLER, _SINKS
    policy = load_logging_policy(policy_path) if policy_path is not None else None
    resolved_level = level if level is not None else policy.level if policy else logging.INFO
    resolved_log_dir = log_dir.resolve() if log_dir is not None else None
    resolved_policy_path = policy_path.resolve() if policy_path is not None else None
    file_sink_enabled = bool(
        resolved_log_dir is not None
        and policy is not None
        and (create_log_dir or resolved_log_dir.is_dir())
    )
    key = (
        str(resolved_log_dir) if resolved_log_dir is not None else None,
        str(resolved_policy_path) if resolved_policy_path is not None else None,
        resolved_level,
        file_sink_enabled,
        id(sys.stderr),
        policy,
    )
    with _CONFIG_LOCK:
        if _CONFIGURED_KEY == key:
            log_file = (
                resolved_log_dir / policy.file_name
                if file_sink_enabled and resolved_log_dir is not None and policy is not None
                else None
            )
            return LoggingConfiguration(policy, file_sink_enabled, log_file)
        _shutdown_logging_locked()

        formatter = JsonFormatter(policy)
        stderr = logging.StreamHandler()
        stderr.setFormatter(formatter)
        sinks: list[logging.Handler] = [stderr]
        log_file: Path | None = None

        if file_sink_enabled:
            assert resolved_log_dir is not None
            assert policy is not None
            if create_log_dir:
                resolved_log_dir.mkdir(parents=True, exist_ok=True)
            log_file = resolved_log_dir / policy.file_name
            _recover_corrupt_tail(log_file)
            _prune_expired_logs(log_file, retention_days=policy.retention_days)
            _enforce_log_budget(log_file, max_total_bytes=policy.maximum_file_bytes)
            file_handler = DailySizeRotatingFileHandler(log_file, policy=policy)
            file_handler.setFormatter(formatter)
            sinks.append(file_handler)

        event_queue: queue.Queue[logging.LogRecord] = queue.Queue(
            maxsize=policy.queue_size if policy is not None else 1024
        )
        queue_handler = BoundedQueueHandler(event_queue, stderr)
        logger = logging.getLogger(_LOGGER_NAME)
        logger.handlers.clear()
        logger.addHandler(queue_handler)
        logger.setLevel(resolved_level)
        logger.propagate = False
        logger.disabled = False

        listener = logging.handlers.QueueListener(
            event_queue,
            *sinks,
            respect_handler_level=True,
        )
        listener.start()
        _LISTENER = listener
        _EVENT_QUEUE = event_queue
        _QUEUE_HANDLER = queue_handler
        _SINKS = sinks
        _CONFIGURED_KEY = key
        return LoggingConfiguration(policy, file_sink_enabled, log_file)


def configure_project_logging(
    project_root: Path,
    runtime_root: Path,
    *,
    create_log_dir: bool,
) -> LoggingConfiguration:
    """Load the project policy and configure the standard runtime log location."""

    return configure_logging(
        log_dir=runtime_root / "logs",
        policy_path=project_root / "configs" / "logging_policy.yaml",
        create_log_dir=create_log_dir,
    )


def flush_logging() -> None:
    """Wait for queued records and flush active sinks without reconfiguration."""

    event_queue = _EVENT_QUEUE
    if event_queue is not None:
        event_queue.join()
    for sink in tuple(_SINKS):
        sink.flush()


def shutdown_logging() -> None:
    with _CONFIG_LOCK:
        _shutdown_logging_locked()


def _shutdown_logging_locked() -> None:
    global _CONFIGURED_KEY, _EVENT_QUEUE, _LISTENER, _QUEUE_HANDLER, _SINKS
    listener = _LISTENER
    _LISTENER = None
    if listener is not None:
        listener.stop()
    _EVENT_QUEUE = None

    logger = logging.getLogger(_LOGGER_NAME)
    if _QUEUE_HANDLER is not None and _QUEUE_HANDLER in logger.handlers:
        logger.removeHandler(_QUEUE_HANDLER)
    if _QUEUE_HANDLER is not None:
        _QUEUE_HANDLER.close()
    _QUEUE_HANDLER = None
    for sink in _SINKS:
        sink.close()
    _SINKS = []
    logger.propagate = True
    _CONFIGURED_KEY = None


def logging_is_configured() -> bool:
    return _CONFIGURED_KEY is not None


def new_correlation_id() -> str:
    return f"corr-{uuid4().hex}"


def bind_operational_context(
    *,
    correlation_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
) -> OperationalContext:
    """Bind a fresh top-level operation context for the current execution context."""

    resolved = OperationalContext(
        correlation_id=correlation_id or new_correlation_id(),
        run_id=run_id,
        request_id=request_id,
    )
    _CORRELATION_ID.set(resolved.correlation_id)
    _RUN_ID.set(run_id)
    _REQUEST_ID.set(request_id)
    return resolved


def clear_operational_context() -> None:
    _CORRELATION_ID.set(None)
    _RUN_ID.set(None)
    _REQUEST_ID.set(None)


@contextmanager
def operational_scope(
    *,
    correlation_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
) -> Iterator[OperationalContext]:
    resolved = OperationalContext(
        correlation_id=correlation_id or _CORRELATION_ID.get() or new_correlation_id(),
        run_id=run_id if run_id is not None else _RUN_ID.get(),
        request_id=request_id if request_id is not None else _REQUEST_ID.get(),
    )
    tokens: tuple[Token[str | None], Token[str | None], Token[str | None]] = (
        _CORRELATION_ID.set(resolved.correlation_id),
        _RUN_ID.set(resolved.run_id),
        _REQUEST_ID.set(resolved.request_id),
    )
    try:
        yield resolved
    finally:
        _REQUEST_ID.reset(tokens[2])
        _RUN_ID.reset(tokens[1])
        _CORRELATION_ID.reset(tokens[0])


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    with operational_scope(correlation_id=correlation_id) as context:
        yield context.correlation_id


def current_correlation_id() -> str:
    existing = _CORRELATION_ID.get()
    if existing:
        return existing
    return bind_operational_context().correlation_id


def current_run_id() -> str | None:
    return _RUN_ID.get()


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def emit_operational_event(
    *,
    component: str,
    event: str,
    event_kind: OperationalEventKind = OperationalEventKind.OPERATIONAL,
    severity: OperationalSeverity = OperationalSeverity.INFO,
    failure_class: str | None = None,
    correlation_id: str | None = None,
    run_id: str | None = None,
    request_id: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> StructuredLogEvent:
    structured = StructuredLogEvent(
        correlation_id=correlation_id or current_correlation_id(),
        event_kind=event_kind,
        component=component,
        event=event,
        severity=severity,
        failure_class=failure_class,
        run_id=run_id if run_id is not None else current_run_id(),
        request_id=request_id if request_id is not None else current_request_id(),
        context=dict(context or {}),
    )
    emit_structured_event(structured)
    return structured


def emit_structured_event(event: StructuredLogEvent) -> None:
    if not logging_is_configured():
        return
    logging.getLogger("astock.operational").log(
        getattr(logging, event.severity.value),
        event.event,
        extra={"structured_event": event.model_dump(mode="json")},
    )


def sanitize_for_log(value: Any, *, policy: LoggingPolicy | None = None) -> Any:
    """Recursively redact and bound values before they reach any logging sink."""

    seen: set[int] = set()
    return _sanitize_for_log(value, policy=policy, depth=0, seen=seen)


def _sanitize_for_log(
    value: Any,
    *,
    policy: LoggingPolicy | None,
    depth: int,
    seen: set[int],
) -> Any:
    max_depth = policy.max_context_depth if policy else _DEFAULT_MAX_CONTEXT_DEPTH
    max_items = policy.max_context_items if policy else _DEFAULT_MAX_CONTEXT_ITEMS
    if depth > max_depth:
        return "<truncated-depth>"

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            return "<cycle>"
        seen.add(identity)
        try:
            result: dict[str, Any] = {}
            items = list(value.items())
            for raw_key, child in items[:max_items]:
                key = str(raw_key)
                normalized = key.casefold()
                if _is_redacted_key(normalized, policy):
                    result[key] = "[REDACTED]"
                elif _is_body_key(normalized, policy):
                    result[key] = "[REDACTED_BODY]"
                else:
                    result[key] = _sanitize_for_log(
                        child,
                        policy=policy,
                        depth=depth + 1,
                        seen=seen,
                    )
            if len(items) > max_items:
                result["_truncated_items"] = len(items) - max_items
            return result
        finally:
            seen.remove(identity)

    if (
        isinstance(value, (set, frozenset))
        or isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray, memoryview))
    ):
        identity = id(value)
        if identity in seen:
            return "<cycle>"
        seen.add(identity)
        try:
            items = (
                sorted(value, key=repr)
                if isinstance(value, (set, frozenset))
                else list(value)
            )
            list_result = [
                _sanitize_for_log(item, policy=policy, depth=depth + 1, seen=seen)
                for item in items[:max_items]
            ]
            if len(items) > max_items:
                list_result.append(f"<truncated-items:{len(items) - max_items}>")
            return list_result
        finally:
            seen.remove(identity)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary:{len(value)} bytes>"
    if isinstance(value, Path):
        return _bounded_text(
            redact_text(
                str(value),
                redact_private_paths=policy is None or policy.redact_private_paths,
            ),
            policy,
        )
    if isinstance(value, str):
        return _bounded_text(
            redact_text(
                value,
                redact_private_paths=policy is None or policy.redact_private_paths,
            ),
            policy,
        )
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _bounded_text(
        redact_text(
            str(value),
            redact_private_paths=policy is None or policy.redact_private_paths,
        ),
        policy,
    )


def _is_redacted_key(normalized: str, policy: LoggingPolicy | None) -> bool:
    return bool(
        (policy is not None and normalized in policy.redacted_keys)
        or _SECRET_KEY_RE.search(normalized)
    )


def _is_body_key(normalized: str, policy: LoggingPolicy | None) -> bool:
    return bool(
        (policy is None or policy.redact_request_bodies)
        and normalized in _REQUEST_BODY_KEYS
    )


def redact_text(value: str, *, redact_private_paths: bool = True) -> str:
    def _header_replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}: [REDACTED]"

    def _secret_replacement(match: re.Match[str]) -> str:
        return match.group(1) + "[REDACTED]"

    text = _HEADER_SECRET_RE.sub(_header_replacement, value)
    text = _SECRET_VALUE_RE.sub(_secret_replacement, text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    if redact_private_paths:
        for pattern in _PRIVATE_PATH_RES:
            text = pattern.sub("<private-user-path>", text)
    return text


def _bounded_text(value: str, policy: LoggingPolicy | None) -> str:
    maximum = policy.max_message_chars if policy else _DEFAULT_MAX_MESSAGE_CHARS
    if len(value) <= maximum:
        return value
    omitted = len(value) - maximum
    return f"{value[:maximum]}<truncated-chars:{omitted}>"


def _bounded_json(payload: dict[str, Any], policy: LoggingPolicy | None) -> str:
    maximum = policy.max_event_bytes if policy else _DEFAULT_MAX_EVENT_BYTES
    rendered = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(rendered.encode("utf-8")) <= maximum:
        return rendered

    event = payload.get("event")
    compact_event: dict[str, Any] | None = None
    if isinstance(event, Mapping):
        compact_event = {
            key: event.get(key)
            for key in (
                "schema_version",
                "created_at",
                "correlation_id",
                "event_kind",
                "component",
                "event",
                "severity",
                "failure_class",
                "run_id",
                "request_id",
            )
            if event.get(key) is not None
        }
        compact_event["context"] = {"_truncated": True}
    compact = {
        key: payload[key]
        for key in (
            "timestamp",
            "level",
            "logger",
            "correlation_id",
            "run_id",
            "request_id",
        )
        if key in payload
    }
    compact["message"] = "operational event exceeded configured size bound"
    if compact_event is not None:
        compact["event"] = compact_event
    rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(rendered.encode("utf-8")) <= maximum:
        return rendered
    return json.dumps(
        {
            "timestamp": payload.get("timestamp"),
            "level": payload.get("level"),
            "logger": payload.get("logger"),
            "message": "operational event omitted: size bound",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _recover_corrupt_tail(path: Path) -> None:
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        return
    try:
        line = _read_last_nonempty_line(path)
        if line is not None:
            json.loads(line.decode("utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        _quarantine_log(path, label="corrupt")


def _read_last_nonempty_line(path: Path, *, max_scan_bytes: int = 1048576) -> bytes | None:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0 and len(buffer) < max_scan_bytes:
            size = min(8192, position)
            position -= size
            handle.seek(position)
            buffer = handle.read(size) + buffer
            lines = [line for line in buffer.splitlines() if line.strip()]
            if len(lines) >= 2 or (position == 0 and lines):
                return lines[-1]
        if buffer.strip():
            raise ValueError("last log record exceeds recovery scan bound")
        return None


def _quarantine_log(path: Path, *, label: str) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine = path.with_name(f"{path.name}.{label}.{stamp}.{uuid4().hex[:8]}")
    try:
        os.replace(path, quarantine)
    except OSError:
        return None
    return quarantine


def _prune_expired_logs(path: Path, *, retention_days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for candidate in _log_family(path, include_active=False):
        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                candidate.unlink(missing_ok=True)
        except OSError:
            continue


def _enforce_log_budget(path: Path, *, max_total_bytes: int) -> None:
    files = _log_family(path, include_active=True)
    sized: list[tuple[Path, int, float]] = []
    for candidate in files:
        try:
            stat = candidate.stat()
        except OSError:
            continue
        sized.append((candidate, stat.st_size, stat.st_mtime))
    total = sum(size for _, size, _ in sized)
    if total <= max_total_bytes:
        return

    deletable = sorted(
        (item for item in sized if item[0] != path),
        key=lambda item: (item[2], item[0].name),
    )
    for candidate, size, _ in deletable:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue
        total -= size
        if total <= max_total_bytes:
            return

    if path.exists() and total > max_total_bytes:
        _quarantine_log(path, label="oversize")


def _log_family(path: Path, *, include_active: bool) -> list[Path]:
    candidates = [item for item in path.parent.glob(f"{path.name}.*") if item.is_file()]
    if include_active and path.is_file():
        candidates.append(path)
    return candidates


atexit.register(shutdown_logging)

__all__ = [
    "BoundedQueueHandler",
    "DailySizeRotatingFileHandler",
    "JsonFormatter",
    "LoggingConfiguration",
    "LoggingPolicy",
    "OperationalContext",
    "bind_operational_context",
    "clear_operational_context",
    "configure_logging",
    "configure_project_logging",
    "correlation_scope",
    "current_correlation_id",
    "current_request_id",
    "current_run_id",
    "emit_operational_event",
    "emit_structured_event",
    "flush_logging",
    "load_logging_policy",
    "logging_is_configured",
    "new_correlation_id",
    "operational_scope",
    "redact_text",
    "sanitize_for_log",
    "shutdown_logging",
]
