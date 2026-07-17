"""Deterministic policy gates applied before durable writes."""

from __future__ import annotations

from dataclasses import dataclass

from astock.core.errors import FailureClass, PolicyError
from astock.schemas import CodexDraft, CommitteeAccessPolicy


@dataclass(frozen=True)
class PolicyEngine:
    """Policy gates for Codex writes and frozen-input committee access."""

    version: str = "m1.2"

    def check_codex_import(self, draft: CodexDraft) -> None:
        if draft.requested_commands:
            raise PolicyError(
                "Codex imports artifacts only; state-changing commands require a dedicated "
                "validated CLI and real brokerage orders are never supported.",
                failure_class=FailureClass.POLICY_REJECTED,
                details={"requested_commands": draft.requested_commands},
            )

    def check_committee_access(
        self,
        policy: CommitteeAccessPolicy,
        *,
        expected_hashes: list[str],
    ) -> None:
        forbidden_access = any(
            (
                policy.network_access,
                policy.api_access,
                policy.mcp_access,
                policy.browser_access,
                policy.full_document_access,
                policy.new_research_allowed,
            )
        )
        if (
            forbidden_access
            or policy.frozen_artifact_hashes != sorted(set(expected_hashes))
            or policy.missing_evidence_action != "NEEDS_INFO"
            or not policy.investigation_task_required
        ):
            raise PolicyError(
                "Committee access must be offline, frozen-input-only, and fail to NEEDS_INFO.",
                failure_class=FailureClass.POLICY_REJECTED,
                details={"expected_frozen_input_count": len(set(expected_hashes))},
            )
