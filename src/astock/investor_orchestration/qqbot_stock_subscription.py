"""Lightweight transport from ChatGPT/AStock reports into QQBot's stock subscription inbox.

This module deliberately does not participate in AStock's scheduled-notification outbox:
ChatGPT Scheduled Tasks are the report clock, and QQBot's existing FeedRunner owns
subscription polling/dedup/delivery after the report reaches the remote inbox.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class StockSubscriptionSubmitStatus(StrEnum):
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class QQBotStockSubscriptionConfig:
    version: str
    transport: str
    ssh_host: str
    identity_file: str
    known_hosts_file: str
    remote_command: str
    connect_timeout_seconds: int
    command_timeout_seconds: int

    @classmethod
    def load(cls, path: Path) -> QQBotStockSubscriptionConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if raw.get("version") != "qqbot-stock-subscription-v1":
            raise ValueError("unsupported QQBot stock subscription config version")
        if raw.get("transport") != "ssh":
            raise ValueError("QQBot stock subscription transport must be ssh")
        host = str(raw.get("ssh_host") or "").strip()
        identity_file = str(raw.get("identity_file") or "").strip()
        known_hosts_file = str(raw.get("known_hosts_file") or "").strip()
        command = str(raw.get("remote_command") or "").strip()
        if not host or not command:
            raise ValueError("QQBot stock subscription ssh_host/remote_command are required")
        connect_timeout = int(raw.get("connect_timeout_seconds") or 0)
        command_timeout = int(raw.get("command_timeout_seconds") or 0)
        if not 1 <= connect_timeout <= 60:
            raise ValueError("connect_timeout_seconds must be within [1, 60]")
        if not 1 <= command_timeout <= 300:
            raise ValueError("command_timeout_seconds must be within [1, 300]")
        return cls(
            version=str(raw["version"]),
            transport="ssh",
            ssh_host=host,
            identity_file=identity_file,
            known_hosts_file=known_hosts_file,
            remote_command=command,
            connect_timeout_seconds=connect_timeout,
            command_timeout_seconds=command_timeout,
        )


@dataclass(frozen=True)
class StockSubscriptionSubmitResult:
    status: StockSubscriptionSubmitStatus
    report_id: str
    detail: str = ""
    remote_file: str = ""
    return_code: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "report_id": self.report_id,
            "detail": self.detail,
            "remote_file": self.remote_file,
            "return_code": self.return_code,
        }


def build_stock_subscription_item(
    text: str,
    *,
    report_id: str | None = None,
    created_at: datetime | None = None,
) -> dict[str, str]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("stock subscription report text must not be empty")
    now = created_at or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("stock subscription created_at must be timezone-aware")
    ident = str(report_id or "").strip()
    if not ident:
        ident = f"astock-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    if len(ident) > 200:
        raise ValueError("stock subscription report_id must be at most 200 characters")
    return {
        "id": ident,
        "created_at": now.isoformat(),
        "date": now.isoformat(),
        "full_text": body,
    }


class QQBotStockSubscriptionSubmitter:
    """One-shot SSH transport. Ambiguous failures are never automatically retried."""

    def __init__(
        self,
        config: QQBotStockSubscriptionConfig,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self.config = config
        self._runner = runner

    def submit(
        self,
        text: str,
        *,
        report_id: str | None = None,
        created_at: datetime | None = None,
    ) -> StockSubscriptionSubmitResult:
        item = build_stock_subscription_item(
            text,
            report_id=report_id,
            created_at=created_at,
        )
        payload = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        args = ["ssh", "-T"]
        if self.config.identity_file:
            args.extend(["-i", self.config.identity_file])
        if self.config.known_hosts_file:
            args.extend(
                [
                    "-o",
                    f"UserKnownHostsFile={self.config.known_hosts_file}",
                    "-o",
                    "StrictHostKeyChecking=accept-new",
                ]
            )
        args.extend(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={self.config.connect_timeout_seconds}",
                "-o",
                "ConnectionAttempts=1",
                self.config.ssh_host,
                self.config.remote_command,
            ]
        )
        try:
            proc = self._runner(
                args,
                input=payload,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return StockSubscriptionSubmitResult(
                status=StockSubscriptionSubmitStatus.UNKNOWN,
                report_id=item["id"],
                detail=(
                    "SSH command timed out; remote delivery state is unknown "
                    "and was not retried"
                ),
            )
        except OSError as exc:
            return StockSubscriptionSubmitResult(
                status=StockSubscriptionSubmitStatus.FAILED,
                report_id=item["id"],
                detail=f"SSH process could not start: {type(exc).__name__}: {exc}",
            )

        stdout = str(proc.stdout or "").strip()
        stderr = str(proc.stderr or "").strip()
        if int(proc.returncode) != 0:
            return StockSubscriptionSubmitResult(
                status=StockSubscriptionSubmitStatus.FAILED,
                report_id=item["id"],
                detail=(stderr or stdout or "remote submit command failed")[:500],
                return_code=int(proc.returncode),
            )
        try:
            receipt = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return StockSubscriptionSubmitResult(
                status=StockSubscriptionSubmitStatus.UNKNOWN,
                report_id=item["id"],
                detail="remote submit returned an unreadable receipt; not retried",
                return_code=0,
            )
        if not isinstance(receipt, dict) or receipt.get("ok") is not True:
            return StockSubscriptionSubmitResult(
                status=StockSubscriptionSubmitStatus.FAILED,
                report_id=item["id"],
                detail=str(receipt.get("error") if isinstance(receipt, dict) else receipt)[:500],
                return_code=0,
            )
        return StockSubscriptionSubmitResult(
            status=StockSubscriptionSubmitStatus.SENT,
            report_id=item["id"],
            remote_file=str(receipt.get("file") or ""),
            return_code=0,
        )


__all__ = [
    "QQBotStockSubscriptionConfig",
    "QQBotStockSubscriptionSubmitter",
    "StockSubscriptionSubmitResult",
    "StockSubscriptionSubmitStatus",
    "build_stock_subscription_item",
]
