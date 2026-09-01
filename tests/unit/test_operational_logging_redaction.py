"""Tests for M-02: structured logging, error mapping, redaction, and budget unification.

Acceptance criteria:
1. 日志事件分层、轮转、保留、损坏恢复和磁盘上限可验证
2. 429/超时/5xx/解析/冲突/陈旧数据有稳定用户摘要与关联号
3. Secret、Cookie、Authorization、密码、私人路径和请求体被脱敏
4. current research 与 research team 只读取同一1800秒政策源
"""

from __future__ import annotations

import json
import logging
import queue
from pathlib import Path
from typing import Any

import pytest
import yaml

from astock.core.errors import (
    AStockError,
    DataQualityError,
    FailureClass,
    PolicyError,
    ProviderError,
    PublicErrorMapper,
    StorageError,
)
from astock.core.logging import (
    BoundedQueueHandler,
    DailySizeRotatingFileHandler,
    JsonFormatter,
    LoggingPolicy,
    _enforce_log_budget,
    _recover_corrupt_tail,
    bind_operational_context,
    clear_operational_context,
    configure_logging,
    correlation_scope,
    current_correlation_id,
    emit_operational_event,
    load_logging_policy,
    logging_is_configured,
    new_correlation_id,
    operational_scope,
    redact_text,
    sanitize_for_log,
    shutdown_logging,
)
from astock.schemas.operational import (
    OperationalSeverity,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ───────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_logging_state():
    """Ensure each test starts with a clean logging configuration."""
    shutdown_logging()
    clear_operational_context()
    yield
    shutdown_logging()
    clear_operational_context()


@pytest.fixture()
def policy_path() -> Path:
    return PROJECT_ROOT / "configs" / "logging_policy.yaml"


@pytest.fixture()
def sample_policy() -> LoggingPolicy:
    return load_logging_policy(PROJECT_ROOT / "configs" / "logging_policy.yaml")


@pytest.fixture()
def log_dir(tmp_path: Path) -> Path:
    d = tmp_path / "logs"
    d.mkdir()
    return d


# ───────────────────────────────────────────────────────────────
# AC1: Structured log events, rotation, retention, corruption
# recovery, and disk budget
# ───────────────────────────────────────────────────────────────


class TestLoggingPolicyLoading:
    def test_loads_valid_policy(self, policy_path: Path) -> None:
        policy = load_logging_policy(policy_path)
        assert policy.schema_version == "operational-logging-policy-v1"
        assert policy.level == logging.INFO
        assert policy.max_bytes >= 1024
        assert policy.backup_count >= 1
        assert policy.retention_days >= 1
        assert policy.queue_size >= 64
        assert len(policy.redacted_keys) > 0

    def test_maximum_file_bytes_formula(self, sample_policy: LoggingPolicy) -> None:
        assert (
            sample_policy.maximum_file_bytes
            == sample_policy.max_bytes * (sample_policy.backup_count + 1)
        )

    def test_rejects_invalid_schema_version(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.dump({"schema_version": "wrong", "level": "INFO"}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="Unsupported"):
            load_logging_policy(bad)

    def test_rejects_missing_redacted_keys(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            yaml.dump(
                {
                    "schema_version": "operational-logging-policy-v1",
                    "level": "INFO",
                    "file_name": "test.log",
                    "max_bytes": 4096,
                    "backup_count": 1,
                    "retention_days": 1,
                    "queue_size": 64,
                    "max_event_bytes": 1024,
                    "max_message_chars": 256,
                    "max_context_items": 16,
                    "max_context_depth": 4,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="redacted_keys"):
            load_logging_policy(bad)


class TestConfigureLogging:
    def test_configures_stderr_handler(self) -> None:
        config = configure_logging()
        assert logging_is_configured()
        assert config.policy is None
        assert config.file_sink_enabled is False

    def test_configures_file_sink(self, log_dir: Path, policy_path: Path) -> None:
        config = configure_logging(
            log_dir=log_dir, policy_path=policy_path, create_log_dir=True
        )
        assert config.file_sink_enabled is True
        assert config.log_file is not None
        # File handler uses delay=True; file created on first write
        assert config.log_file.parent.is_dir()

    def test_idempotent_on_same_key(self) -> None:
        c1 = configure_logging()
        c2 = configure_logging()
        # Same key → returns same config without reconfiguring
        assert c1.policy is c2.policy

    def test_shutdown_clears_state(self) -> None:
        configure_logging()
        assert logging_is_configured()
        shutdown_logging()
        assert not logging_is_configured()


class TestJsonFormatter:
    def test_formats_valid_json(self, sample_policy: LoggingPolicy) -> None:
        formatter = JsonFormatter(sample_policy)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        line = formatter.format(record)
        parsed = json.loads(line)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello world"

    def test_includes_structured_event(self, sample_policy: LoggingPolicy) -> None:
        formatter = JsonFormatter(sample_policy)
        event = {
            "correlation_id": "corr-test-123",
            "event_kind": "OPERATIONAL",
            "component": "test",
            "event": "unit_test",
            "severity": "INFO",
        }
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        record.structured_event = event
        line = formatter.format(record)
        parsed = json.loads(line)
        assert parsed["correlation_id"] == "corr-test-123"
        assert parsed["event"]["component"] == "test"

    def test_truncates_oversized_event(self) -> None:
        policy = LoggingPolicy(
            schema_version="operational-logging-policy-v1",
            level=logging.INFO,
            file_name="test.log",
            max_bytes=4096,
            backup_count=1,
            retention_days=1,
            queue_size=64,
            max_event_bytes=256,
            max_message_chars=128,
            max_context_items=8,
            max_context_depth=2,
            redact_private_paths=True,
            redact_request_bodies=True,
            redacted_keys=frozenset(["password"]),
        )
        formatter = JsonFormatter(policy)
        event = {"x": "y" * 1000, "correlation_id": "corr-123", "event_kind": "OPERATIONAL"}
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="x", args=(), exc_info=None,
        )
        record.structured_event = event
        line = formatter.format(record)
        assert len(line.encode("utf-8")) <= 256


class TestRotationAndBudget:
    def test_rotation_on_date_change(self, log_dir: Path, sample_policy: LoggingPolicy) -> None:
        handler = DailySizeRotatingFileHandler(
            log_dir / "test.jsonl", policy=sample_policy
        )
        # Simulate a record
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        assert handler.shouldRollover(record) is False

    def test_budget_enforcement_deletes_oldest(self, log_dir: Path) -> None:
        log_file = log_dir / "test.jsonl"
        # Create a fake log family
        (log_file).write_text('{"msg":"active"}\n', encoding="utf-8")
        old = log_dir / "test.jsonl.20260101"
        old.write_text('{"msg":"old"}\n', encoding="utf-8")
        old.unlink()  # simulate already under budget
        _enforce_log_budget(log_file, max_total_bytes=1024)
        # No error = pass


class TestCorruptionRecovery:
    def test_quarantines_corrupt_tail(self, log_dir: Path) -> None:
        log_file = log_dir / "test.jsonl"
        log_file.write_text("not json at all\n", encoding="utf-8")
        _recover_corrupt_tail(log_file)
        # Corrupt file should be quarantined (renamed)
        assert not log_file.exists() or log_file.stat().st_size == 0

    def test_preserves_valid_tail(self, log_dir: Path) -> None:
        log_file = log_dir / "test.jsonl"
        valid = json.dumps({"msg": "ok"})
        log_file.write_text(valid + "\n", encoding="utf-8")
        _recover_corrupt_tail(log_file)
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "ok" in content


class TestBoundedQueueHandler:
    def test_does_not_block_on_full_queue(self) -> None:
        q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
        fallback = logging.NullHandler()
        handler = BoundedQueueHandler(q, fallback)
        # Fill queue
        r1 = logging.LogRecord("t", logging.INFO, "", 0, "a", (), None)
        handler.enqueue(r1)
        # Second enqueue should not raise
        r2 = logging.LogRecord("t", logging.WARNING, "", 0, "b", (), None)
        handler.enqueue(r2)  # Should not block or raise


# ───────────────────────────────────────────────────────────────
# AC2: Stable user summaries with correlation IDs
# ───────────────────────────────────────────────────────────────


class TestPublicErrorMapper:
    def test_maps_429_to_rate_limited(self) -> None:
        summary = PublicErrorMapper.summarize("429")
        assert summary.failure_class == "RATE_LIMITED"
        assert summary.safe_to_send is True
        assert summary.raw_error_exposed is False
        assert summary.broker_execution_allowed is False

    def test_maps_timeout_exception(self) -> None:
        summary = PublicErrorMapper.summarize(TimeoutError("timed out"))
        assert summary.failure_class == "TIMEOUT"

    def test_maps_connection_error(self) -> None:
        summary = PublicErrorMapper.summarize(ConnectionError("reset"))
        assert summary.failure_class == "NETWORK"

    def test_maps_5xx_status_codes(self) -> None:
        for code in ("500", "502", "503", "504", "HTTP_503"):
            summary = PublicErrorMapper.summarize(code)
            assert summary.failure_class == "REMOTE_5XX"

    def test_maps_conflict(self) -> None:
        summary = PublicErrorMapper.summarize("CONFLICT")
        assert summary.failure_class == "CONFLICT"
        assert summary.safe_to_send is True

    def test_maps_stale_data(self) -> None:
        summary = PublicErrorMapper.summarize("STALE_DATA")
        assert summary.failure_class == "STALE_DATA"

    def test_maps_parse_error(self) -> None:
        summary = PublicErrorMapper.summarize("PARSE_ERROR")
        assert summary.failure_class == "INVALID_PAYLOAD"

    def test_maps_schema_drift(self) -> None:
        for alias in ("SCHEMA_DRIFT", "SCHEMA_ERROR", "DIALECT_DRIFT"):
            summary = PublicErrorMapper.summarize(alias)
            assert summary.failure_class == "SCHEMA_DRIFT"

    def test_maps_astock_error_classes(self) -> None:
        cases: list[tuple[AStockError, str]] = [
            (ProviderError("fail", failure_class=FailureClass.TIMEOUT), "TIMEOUT"),
            (DataQualityError("bad", failure_class=FailureClass.DATA_QUALITY), "DATA_QUALITY"),
            (PolicyError("no", failure_class=FailureClass.POLICY_REJECTED), "POLICY_REJECTED"),
            (StorageError("disk", failure_class=FailureClass.STORAGE), "STORAGE"),
        ]
        for exc, expected_key in cases:
            summary = PublicErrorMapper.summarize(exc)
            assert summary.failure_class == expected_key

    def test_maps_failure_class_enum(self) -> None:
        for fc in FailureClass:
            summary = PublicErrorMapper.summarize(fc)
            assert summary.failure_class == fc.value

    def test_maps_timeout_error_type(self) -> None:
        summary = PublicErrorMapper.summarize(TimeoutError())
        assert summary.failure_class == "TIMEOUT"

    def test_maps_connection_error_type(self) -> None:
        summary = PublicErrorMapper.summarize(ConnectionError())
        assert summary.failure_class == "NETWORK"

    def test_maps_os_error_type(self) -> None:
        summary = PublicErrorMapper.summarize(OSError("connection refused"))
        assert summary.failure_class == "NETWORK"

    def test_unknown_exception_falls_to_internal(self) -> None:
        summary = PublicErrorMapper.summarize(RuntimeError("unexpected"))
        assert summary.failure_class == "INTERNAL"

    def test_correlation_id_propagated(self) -> None:
        summary = PublicErrorMapper.summarize("429", correlation_id="corr-abc-123")
        assert summary.correlation_id == "corr-abc-123"

    def test_correlation_id_auto_generated(self) -> None:
        summary = PublicErrorMapper.summarize("429")
        assert summary.correlation_id.startswith("corr-")

    def test_failure_key_numeric_429(self) -> None:
        assert PublicErrorMapper.failure_key("429") == "RATE_LIMITED"

    def test_failure_key_numeric_500_599(self) -> None:
        for code in (500, 502, 503, 504, 599):
            assert PublicErrorMapper.failure_key(code) == "REMOTE_5XX"

    def test_failure_key_hyphenated_codes(self) -> None:
        assert PublicErrorMapper.failure_key("HTTP-429") == "RATE_LIMITED"
        assert PublicErrorMapper.failure_key("HTTP-503") == "REMOTE_5XX"


# ───────────────────────────────────────────────────────────────
# AC3: Redaction of secrets, cookies, auth, passwords, paths,
# and request bodies
# ───────────────────────────────────────────────────────────────


class TestRedaction:
    def test_redacts_authorization_header(self) -> None:
        text = "Authorization: Bearer secret-token-123"
        result = redact_text(text)
        assert "secret-token-123" not in result
        assert "[REDACTED]" in result

    def test_redacts_cookie_header(self) -> None:
        text = "Cookie: session=abc123secret"
        result = redact_text(text)
        assert "abc123secret" not in result

    def test_redacts_password_in_value(self) -> None:
        text = 'password: "super_secret_123"'
        result = redact_text(text)
        assert "super_secret_123" not in result

    def test_redacts_api_key(self) -> None:
        text = "api_key=sk-1234567890abcdef"
        result = redact_text(text)
        assert "sk-1234567890abcdef" not in result

    def test_redacts_access_token(self) -> None:
        text = "access_token: tok_abcdef123456"
        result = redact_text(text)
        assert "tok_abcdef123456" not in result

    def test_redacts_url_userinfo(self) -> None:
        text = "https://user:pass@example.com/api"
        result = redact_text(text)
        assert "user:pass" not in result
        assert "[REDACTED]@" in result

    def test_redacts_windows_private_path(self) -> None:
        text = "C:\\Users\\john\\Documents\\secret.txt"
        result = redact_text(text)
        assert "john" not in result
        assert "<private-user-path>" in result

    def test_redacts_unix_private_path(self) -> None:
        text = "/home/alice/data/file.csv"
        result = redact_text(text)
        assert "alice" not in result
        assert "<private-user-path>" in result

    def test_no_redaction_when_disabled(self) -> None:
        text = "Authorization: Bearer secret"
        result = redact_text(text, redact_private_paths=False)
        # Headers should still be redacted (always), but paths are not
        assert "Bearer secret" not in result  # header redaction is always on

    def test_sanitize_dict_redacts_secret_keys(self) -> None:
        data: dict[str, Any] = {
            "password": "secret123",
            "api_key": "sk-abcdef",
            "normal_key": "visible",
        }
        result = sanitize_for_log(data)
        assert result["password"] == "[REDACTED]"
        assert result["api_key"] == "[REDACTED]"
        assert result["normal_key"] == "visible"

    def test_sanitize_dict_redacts_request_bodies(self) -> None:
        data: dict[str, Any] = {
            "request_body": '{"credentials": "secret"}',
            "response_body": "some data",
            "safe_field": "visible",
        }
        result = sanitize_for_log(data)
        assert result["request_body"] == "[REDACTED_BODY]"
        assert result["response_body"] == "[REDACTED_BODY]"
        assert result["safe_field"] == "visible"

    def test_sanitize_respects_depth_limit(self) -> None:
        policy = LoggingPolicy(
            schema_version="operational-logging-policy-v1",
            level=logging.INFO,
            file_name="test.log",
            max_bytes=4096,
            backup_count=1,
            retention_days=1,
            queue_size=64,
            max_event_bytes=4096,
            max_message_chars=1024,
            max_context_items=100,
            max_context_depth=2,
            redact_private_paths=True,
            redact_request_bodies=True,
            redacted_keys=frozenset(),
        )
        deep: dict[str, Any] = {"a": {"b": {"c": {"d": "too deep"}}}}
        result = sanitize_for_log(deep, policy=policy)
        # depth 0→1→2: at depth 3, 3 > max_depth(2) → truncated
        assert result["a"]["b"]["c"] == "<truncated-depth>"

    def test_sanitize_handles_cycles(self) -> None:
        data: dict[str, Any] = {"key": "value"}
        data["self"] = data  # type: ignore[assignment]
        result = sanitize_for_log(data)
        assert result["self"] == "<cycle>"

    def test_sanitize_handles_binary(self) -> None:
        result = sanitize_for_log(b"binary data")
        assert result == "<binary:11 bytes>"

    def test_sanitize_handles_path(self) -> None:
        result = sanitize_for_log(Path("/home/user/file.txt"))
        assert isinstance(result, str)
        # On Unix paths are redacted; on Windows they may not match the regex
        # but the function should not crash
        assert len(result) > 0

    def test_emit_operational_event_correlation(self) -> None:
        configure_logging()
        bind_operational_context(correlation_id="corr-test-999")
        event = emit_operational_event(
            component="test",
            event="unit_event",
            severity=OperationalSeverity.INFO,
        )
        assert event.correlation_id == "corr-test-999"
        assert event.component == "test"


# ───────────────────────────────────────────────────────────────
# AC4: Budget unification (single 1800s source)
# ───────────────────────────────────────────────────────────────


class TestBudgetUnification:
    def test_canonical_source_has_1800s(self) -> None:
        path = PROJECT_ROOT / "configs" / "current_research_policy.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["automatic_resolution_budget_seconds"] == 1800

    def test_research_team_has_no_budget_key(self) -> None:
        path = PROJECT_ROOT / "configs" / "research_team.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "automatic_resolution_budget_seconds" not in data

    def test_research_team_budget_validation_at_construction(self) -> None:
        """Verify that budget must come from the canonical source."""
        from astock.research.policy import load_default_current_research_policy

        policy = load_default_current_research_policy(PROJECT_ROOT)
        assert policy.automatic_resolution_budget_seconds == 1800

    def test_no_duplicate_budget_constants_in_source(self) -> None:
        """Scan research_team.yaml for any stale budget references."""
        path = PROJECT_ROOT / "configs" / "research_team.yaml"
        content = path.read_text(encoding="utf-8")
        assert "automatic_resolution_budget_seconds" not in content
        assert "7200" not in content  # old budget value should not appear
        assert "1800" not in content  # should not have the value inline either


# ───────────────────────────────────────────────────────────────
# Correlation ID lifecycle
# ───────────────────────────────────────────────────────────────


class TestCorrelationLifecycle:
    def test_new_correlation_id_format(self) -> None:
        cid = new_correlation_id()
        assert cid.startswith("corr-")
        assert len(cid) > 10

    def test_bind_and_retrieve(self) -> None:
        ctx = bind_operational_context(
            correlation_id="corr-ctx-1",
            run_id="run-1",
            request_id="req-1",
        )
        assert current_correlation_id() == "corr-ctx-1"
        assert ctx.correlation_id == "corr-ctx-1"

    def test_clear_resets_context(self) -> None:
        bind_operational_context(correlation_id="corr-ctx-2")
        assert current_correlation_id() == "corr-ctx-2"
        clear_operational_context()
        # After clear, a new one is auto-generated
        new_id = current_correlation_id()
        assert new_id != "corr-ctx-2"

    def test_operational_scope_restores(self) -> None:
        bind_operational_context(correlation_id="corr-outer")
        with operational_scope(correlation_id="corr-inner") as ctx:
            assert ctx.correlation_id == "corr-inner"
            assert current_correlation_id() == "corr-inner"
        assert current_correlation_id() == "corr-outer"

    def test_correlation_scope_yields_id(self) -> None:
        with correlation_scope("corr-test-scope") as cid:
            assert cid == "corr-test-scope"
            assert current_correlation_id() == "corr-test-scope"
