"""Deterministic policy gates applied before durable writes."""

from __future__ import annotations

from dataclasses import dataclass

from astock.core.errors import FailureClass, PolicyError
from astock.schemas import CodexDraft


@dataclass(frozen=True)
class PolicyEngine:
    """M1 policy gate: Codex may import artifacts but may not execute commands."""

    version: str = "m1.1"

    def check_codex_import(self, draft: CodexDraft) -> None:
        if draft.requested_commands:
            raise PolicyError(
                "Codex imports artifacts only; state-changing commands require a dedicated "
                "validated CLI and real brokerage orders are never supported.",
                failure_class=FailureClass.POLICY_REJECTED,
                details={"requested_commands": draft.requested_commands},
            )
