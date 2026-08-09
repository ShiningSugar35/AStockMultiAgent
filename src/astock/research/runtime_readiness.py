"""Read-only operational readiness views for research, holdings, and paper replay."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.paper_trading.ledger import LedgerService
from astock.research.config import load_position_lifecycle_config
from astock.research.knowledge_port import KnowledgeSkillProvider
from astock.research.lifecycle import PositionLifecycleService
from astock.research.trading_classification import TradingClassificationService


class ResearchRuntimeReadinessService:
    """Aggregate existing read-only status APIs without mutating ledger or research state."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        knowledge_provider: KnowledgeSkillProvider,
    ) -> None:
        self.state = state
        self.knowledge_provider = knowledge_provider
        self.classification = TradingClassificationService(state, objects)
        self.holdings = PositionLifecycleService(
            state,
            objects,
            load_position_lifecycle_config(project_root / "configs" / "position_lifecycle.yaml"),
        )
        self.ledger = LedgerService(state, objects)

    def provider_readiness(self, run_id: str) -> dict[str, object]:
        status = self.knowledge_provider.status(run_id)
        return {
            "schema_version": "provider-readiness-v1",
            "knowledge": status.model_dump(mode="json"),
            "broker_execution_allowed": False,
        }

    def holding_due(
        self,
        position_id: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        current = (as_of or datetime.now(UTC)).astimezone(UTC)
        status = self.holdings.status(position_id)
        plan = status.get("plan") if isinstance(status, dict) else None
        if not isinstance(plan, dict):
            return {
                "schema_version": "holding-due-v1",
                "position_id": position_id,
                "status": "NOT_RUN",
                "due": False,
                "next_review_at": None,
                "prepare_action": "CREATE_MONITORING_PLAN",
            }
        raw_next = plan.get("next_review_at")
        next_review = (
            datetime.fromisoformat(str(raw_next)).astimezone(UTC)
            if raw_next
            else None
        )
        due = next_review is None or current >= next_review
        latest_review = status.get("latest_review")
        return {
            "schema_version": "holding-due-v1",
            "position_id": position_id,
            "status": "DUE" if due else "NOT_DUE",
            "plan_id": plan.get("plan_id"),
            "next_review_at": next_review.isoformat() if next_review else None,
            "due": due,
            "latest_review": latest_review,
            "prepare_action": "PREPARE_INCREMENTAL_REVIEW" if due else "NONE",
        }

    def holding_prepare(
        self,
        position_id: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        due = self.holding_due(position_id, as_of=as_of)
        return {
            "schema_version": "holding-prepare-v1",
            "position_id": position_id,
            "status": due["status"],
            "plan_id": due.get("plan_id"),
            "due": due["due"],
            "required_action": due["prepare_action"],
            "write_performed": False,
        }

    def paper_replay_checkpoint(
        self,
        account_id: str,
        symbol: str,
    ) -> dict[str, object]:
        checkpoint = self.ledger.replay_checkpoint(account_id, symbol)
        return {
            "schema_version": "paper-replay-checkpoint-v1",
            "account_id": account_id,
            "symbol": symbol,
            "status": "READY" if checkpoint is not None else "MISSING",
            "checkpoint": (
                checkpoint.model_dump(mode="json") if checkpoint is not None else None
            ),
            "write_performed": False,
        }

    def paper_recovery_plan(
        self,
        account_id: str,
        symbol: str,
    ) -> dict[str, object]:
        checkpoint = self.ledger.replay_checkpoint(account_id, symbol)
        if checkpoint is None:
            action = "INITIALIZE_REPLAY_FROM_VERIFIED_CURSOR"
            reason = "REPLAY_CHECKPOINT_MISSING"
        elif checkpoint.market is None or checkpoint.instrument_id is None:
            action = "RECOVER_FORMAL_REPLAY_IDENTITY"
            reason = "REPLAY_IDENTITY_INCOMPLETE"
        elif checkpoint.missing_bars > 0:
            action = "RECONCILE_MARKET_COVERAGE"
            reason = "REPLAY_COVERAGE_INCOMPLETE"
        else:
            action = "RESUME_FROM_CHECKPOINT"
            reason = "CHECKPOINT_READY"
        return {
            "schema_version": "paper-recovery-plan-v1",
            "account_id": account_id,
            "symbol": symbol,
            "status": "READY" if action == "RESUME_FROM_CHECKPOINT" else "NEEDS_INFO",
            "reason_code": reason,
            "recommended_action": action,
            "checkpoint": (
                checkpoint.model_dump(mode="json") if checkpoint is not None else None
            ),
            "write_performed": False,
        }

    def classification_readiness(
        self,
        artifact_id: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        return {
            "schema_version": "classification-readiness-v1",
            **self.classification.status(artifact_id, as_of=as_of),
        }


__all__ = ["ResearchRuntimeReadinessService"]
