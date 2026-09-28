from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from astock.investor_orchestration.cli import app
from astock.investor_orchestration.qqbot_stock_subscription import (
    QQBotStockSubscriptionConfig,
    QQBotStockSubscriptionSubmitter,
    StockSubscriptionSubmitResult,
    StockSubscriptionSubmitStatus,
    build_stock_subscription_item,
)


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "qqbot.yaml"
    path.write_text(
        "\n".join(
            [
                "version: qqbot-stock-subscription-v1",
                "transport: ssh",
                "ssh_host: test-host",
                "remote_command: bash /srv/qqbot/control/ops/astock_feed_submit.sh",
                "connect_timeout_seconds: 8",
                "command_timeout_seconds: 30",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_build_item_preserves_utf8_multiline_without_hash_contract() -> None:
    text = "【持仓跟踪】\n贵州茅台：继续观察。\n风险：需求恢复慢。"
    item = build_stock_subscription_item(
        text,
        report_id="scheduled-20260928-am",
        created_at=datetime(2026, 9, 28, 1, 30, tzinfo=UTC),
    )

    assert item == {
        "id": "scheduled-20260928-am",
        "created_at": "2026-09-28T01:30:00+00:00",
        "date": "2026-09-28T01:30:00+00:00",
        "full_text": text,
    }
    assert "hash" not in item
    assert "group" not in item


def test_build_item_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_stock_subscription_item("  \n  ")


def test_submit_success_is_one_ssh_call_and_passes_json_on_stdin(tmp_path: Path) -> None:
    cfg = QQBotStockSubscriptionConfig.load(_config(tmp_path))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        payload = json.loads(str(kwargs["input"]))
        assert payload["id"] == "report-1"
        assert payload["full_text"] == "第一行\n第二行"
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"file":"report-test.json"}',
            stderr="",
        )

    result = QQBotStockSubscriptionSubmitter(cfg, runner=run).submit(
        "第一行\n第二行",
        report_id="report-1",
        created_at=datetime(2026, 9, 28, 2, 0, tzinfo=UTC),
    )

    assert result.status is StockSubscriptionSubmitStatus.SENT
    assert result.remote_file == "report-test.json"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:2] == ["ssh", "-T"]
    assert "BatchMode=yes" in args
    assert "ConnectTimeout=8" in args
    assert "ConnectionAttempts=1" in args
    assert args[-2:] == [
        "test-host",
        "bash /srv/qqbot/control/ops/astock_feed_submit.sh",
    ]
    assert kwargs["timeout"] == 30


def test_submit_nonzero_is_failed_without_retry(tmp_path: Path) -> None:
    cfg = QQBotStockSubscriptionConfig.load(_config(tmp_path))
    calls = 0

    def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(returncode=255, stdout="", stderr="connection refused")

    result = QQBotStockSubscriptionSubmitter(cfg, runner=run).submit("正文")

    assert calls == 1
    assert result.status is StockSubscriptionSubmitStatus.FAILED
    assert "connection refused" in result.detail


def test_submit_timeout_is_unknown_without_retry(tmp_path: Path) -> None:
    cfg = QQBotStockSubscriptionConfig.load(_config(tmp_path))
    calls = 0

    def run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    result = QQBotStockSubscriptionSubmitter(cfg, runner=run).submit("正文")

    assert calls == 1
    assert result.status is StockSubscriptionSubmitStatus.UNKNOWN
    assert "not retried" in result.detail


def test_success_exit_with_unreadable_receipt_is_unknown(tmp_path: Path) -> None:
    cfg = QQBotStockSubscriptionConfig.load(_config(tmp_path))

    def run(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    result = QQBotStockSubscriptionSubmitter(cfg, runner=run).submit("正文")

    assert result.status is StockSubscriptionSubmitStatus.UNKNOWN


def test_cli_reads_utf8_file_and_emits_sent_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("第一行\n第二行", encoding="utf-8")
    config = _config(tmp_path)
    captured: dict[str, str | None] = {}

    def fake_submit(self, text: str, *, report_id=None, created_at=None):
        captured["text"] = text
        captured["report_id"] = report_id
        return StockSubscriptionSubmitResult(
            status=StockSubscriptionSubmitStatus.SENT,
            report_id=report_id or "generated",
            remote_file="remote.json",
            return_code=0,
        )

    monkeypatch.setattr(QQBotStockSubscriptionSubmitter, "submit", fake_submit)
    result = CliRunner().invoke(
        app,
        [
            "qqbot-stock-submit",
            str(report),
            "--report-id",
            "scheduled-am",
            "--config-path",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "SENT"
    assert captured == {"text": "第一行\n第二行", "report_id": "scheduled-am"}


def test_cli_non_sent_returns_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "report.txt"
    report.write_text("正文", encoding="utf-8")
    config = _config(tmp_path)

    def fake_submit(self, text: str, *, report_id=None, created_at=None):
        return StockSubscriptionSubmitResult(
            status=StockSubscriptionSubmitStatus.UNKNOWN,
            report_id=report_id or "generated",
            detail="timeout",
        )

    monkeypatch.setattr(QQBotStockSubscriptionSubmitter, "submit", fake_submit)
    result = CliRunner().invoke(
        app,
        [
            "qqbot-stock-submit",
            str(report),
            "--config-path",
            str(config),
        ],
    )

    assert result.exit_code == 4
    assert '"status": "UNKNOWN"' in result.output
