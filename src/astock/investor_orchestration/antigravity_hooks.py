"""Optional Antigravity campaign events over the existing checkpoint and lease.

Only native hook stdin identifies a session. This module never reads transcripts,
calls a model, creates a scheduler, or asks the host to continue its execution loop.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field

from astock.core.hashing import content_hash
from astock.core.state import StateStore
from astock.investor_orchestration.host_continuation import HostContinuationPolicy
from astock.investor_orchestration.models import StrictModel
from astock.investor_orchestration.run_ownership import schedule_run_ownership
from astock.storage_retention import path_has_redirect

if TYPE_CHECKING:
    from astock.investor_orchestration.watch_campaign import CampaignRound

_SCOPE = "antigravity-campaign-session"
_HOOK_NAME = "astock-campaign-observer"
_MAX_INPUT = 65536


class CampaignHostBinding(StrictModel):
    campaign_id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    slot: Literal["am", "pm"]
    owner: str = Field(min_length=1, max_length=200)
    round_id: str = Field(min_length=1, max_length=200)
    lease_generation: int = Field(ge=1)


class CampaignHostStop(StrictModel):
    execution_num: int = Field(ge=0)
    reason: Literal["model_stop", "max_steps_exceeded", "error", "UNKNOWN"]
    fully_idle: bool
    event_id: str
    disposition: str


class CampaignHostSession(StrictModel):
    conversation_id: str
    workspace_root: str
    binding: CampaignHostBinding | None = None
    last_stop: CampaignHostStop | None = None


class AntigravityCampaignEvents:
    def __init__(self, root: Path, database: Path | None = None) -> None:
        self.root = root.resolve()
        path = database or self.root / "runtime" / "state.sqlite"
        if path_has_redirect(self.root, path) or not path.is_file():
            raise ValueError("native hook requires the existing project state")
        self.state = StateStore(path)

    def get(self, conversation_id: str) -> CampaignHostSession | None:
        row = self.state.get_checkpoint(_SCOPE, str(UUID(conversation_id)))
        if row is None:
            return None
        session = CampaignHostSession.model_validate(row["cursor"])
        if session.workspace_root != str(self.root):
            raise ValueError("stored native session belongs to a different workspace")
        return session

    def _save(self, session: CampaignHostSession) -> None:
        self.state.set_checkpoint(
            scope_type=_SCOPE, scope_key=session.conversation_id,
            cursor=session.model_dump(mode="json"), status="OBSERVED",
        )

    def _identity(self, payload: dict[str, object]) -> str:
        identity = payload.get("conversationId")
        workspaces = payload.get("workspacePaths")
        if not isinstance(identity, str) or not isinstance(workspaces, list):
            raise ValueError("native session identity is missing")
        if not 1 <= len(workspaces) <= 32 or not all(
            isinstance(path, str) for path in workspaces
        ):
            raise ValueError("native workspace identities are invalid")
        if self.root not in {Path(path).resolve() for path in workspaces}:
            raise ValueError("native event belongs to a different workspace")
        return str(UUID(identity))

    def session_start(self, payload: dict[str, object]) -> CampaignHostSession:
        identity = self._identity(payload)
        with schedule_run_ownership(self.state.path, _SCOPE, identity):
            previous = self.get(identity)
            if previous is not None:
                return previous
            session = CampaignHostSession(
                conversation_id=identity, workspace_root=str(self.root),
            )
            self._save(session)
            return session

    def bind(self, conversation_id: str, binding: CampaignHostBinding) -> str:
        identity = str(UUID(conversation_id))
        with schedule_run_ownership(self.state.path, _SCOPE, identity):
            session = self.get(identity)
            if session is None:
                return "SESSION_NOT_OBSERVED"
            if session.binding is not None:
                return "BOUND" if session.binding == binding else "ROUND_NOT_BOUND"
            from astock.investor_orchestration.watch_campaign import WatchCampaignService

            service = WatchCampaignService(self.state.path, binding.campaign_id)
            current = service.load().rounds.get(binding.slot)
            if not self._lease_matches(current, binding):
                return "STALE_LEASE"
            self._save(session.model_copy(update={"binding": binding}))
            return "BOUND"

    @staticmethod
    def _lease_matches(current: CampaignRound | None, binding: CampaignHostBinding) -> bool:
        return bool(
            current is not None
            and current.owner == binding.owner
            and current.round_id == binding.round_id
            and current.lease_generation == binding.lease_generation
            and current.deadline_at > datetime.now(UTC)
            and current.status != "COMPLETE"
        )

    @staticmethod
    def _stop(payload: dict[str, object], identity: str) -> CampaignHostStop:
        execution = payload.get("executionNum")
        idle = payload.get("fullyIdle")
        raw_reason = payload.get("terminationReason")
        if type(execution) is not int or execution < 0 or type(idle) is not bool:
            raise ValueError("native Stop requires executionNum and fullyIdle")
        reason = (
            raw_reason
            if isinstance(raw_reason, str)
            and raw_reason in {"model_stop", "max_steps_exceeded", "error"}
            else "UNKNOWN"
        )
        event = {"conversation": identity, "execution": execution, "reason": reason, "idle": idle}
        return CampaignHostStop.model_validate({
            "execution_num": execution, "reason": reason, "fully_idle": idle,
            "event_id": content_hash(event), "disposition": "OBSERVED",
        })

    @staticmethod
    def _out_of_order(previous: CampaignHostStop | None, event: CampaignHostStop) -> bool:
        if previous is None:
            return False
        if event.execution_num != previous.execution_num:
            return event.execution_num < previous.execution_num
        # One execution may first stop while background work remains, then become idle.
        return previous.fully_idle or not event.fully_idle or previous.reason != event.reason

    def stop(self, payload: dict[str, object]) -> str:
        identity = self._identity(payload)
        event = self._stop(payload, identity)
        with schedule_run_ownership(self.state.path, _SCOPE, identity):
            session = self.get(identity)
            if session is None:
                return "SESSION_NOT_OBSERVED"
            if session.last_stop is not None and session.last_stop.event_id == event.event_id:
                return session.last_stop.disposition
            if self._out_of_order(session.last_stop, event):
                return "STALE_EVENT"
            disposition = self._checkpoint_interruption(session.binding, event)
            self._save(session.model_copy(update={
                "last_stop": event.model_copy(update={"disposition": disposition}),
            }))
            return disposition

    def _checkpoint_interruption(
        self, binding: CampaignHostBinding | None, event: CampaignHostStop,
    ) -> str:
        if binding is None:
            return "ROUND_NOT_BOUND"
        from astock.investor_orchestration.watch_campaign import WatchCampaignService

        service = WatchCampaignService(self.state.path, binding.campaign_id)
        current = service.load().rounds.get(binding.slot)
        if not self._lease_matches(current, binding):
            return "STALE_LEASE"
        assert current is not None
        interrupted = event.reason in {"max_steps_exceeded", "error"}
        if not interrupted or not event.fully_idle:
            return "OBSERVED"
        decision = HostContinuationPolicy.evaluate(
            host="OTHER_AGENT",
            elapsed_seconds=max(
                0, int((datetime.now(UTC) - current.invocation_started_at).total_seconds()),
            ),
            completed_units=current.completed_units, total_units=current.total_units,
            existing_platform_task_id=current.child_task_id,
            platform_interrupted=interrupted,
        )
        if not decision.checkpoint_required:
            return "OBSERVED"
        result = service.checkpoint(
            binding.slot, binding.owner, {"status": "PARTIAL"},
            round_id=binding.round_id, lease_generation=binding.lease_generation,
        )
        return result.checkpoint_disposition


def install_hooks(root: Path, *, python: Path | None = None) -> Path:
    """Merge this optional observer into the workspace's native hook configuration."""
    root = root.resolve()
    executable = (python or Path(sys.executable)).absolute()
    if not executable.is_relative_to(root) or not executable.is_file():
        raise ValueError("install with the project's own Python interpreter")
    path = root / ".agents" / "hooks.json"
    if path_has_redirect(root, path):
        raise ValueError("native hook configuration must be project-local")
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(existing, dict):
        raise ValueError("native hooks must be a named object")
    prior = existing.get(_HOOK_NAME)
    if prior is not None and not isinstance(prior, dict):
        raise ValueError("the project's native hook entry is invalid")
    entry = dict(prior or {})
    for event, action in (("SessionStart", "session-start"), ("Stop", "stop")):
        argv = [
            str(executable), "-B", "-m", "astock.investor_orchestration.antigravity_hooks",
            action, "--root", str(root),
        ]
        command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        existing_handlers = entry.get(event, [])
        if not isinstance(existing_handlers, list):
            raise ValueError("native event handlers must be a list")
        retained = [
            item for item in existing_handlers
            if not (
                isinstance(item, dict)
                and "astock.investor_orchestration.antigravity_hooks"
                in str(item.get("command", ""))
            )
        ]
        entry[event] = [*retained, {"type": "command", "command": command, "timeout": 10}]
    entry.setdefault("enabled", True)
    existing[_HOOK_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _hook(action: str, root: Path) -> None:
    try:
        raw = sys.stdin.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            raise ValueError("native hook input is too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("native hook input must be an object")
        service = AntigravityCampaignEvents(root)
        if action == "session-start":
            session = service.session_start(payload)
            metadata = json.dumps({
                "conversationId": session.conversation_id, "workspaceRoot": session.workspace_root,
            }, ensure_ascii=False)
            print(json.dumps({"injectSteps": [{"systemMessage": {
                "systemMessage": "AStock native campaign session metadata: " + metadata,
            }}]}, ensure_ascii=False))
            return
        else:
            service.stop(payload)
    except Exception as exc:  # noqa: BLE001 - optional observer must never stop host work
        print(
            json.dumps({"native_hook": "IGNORED", "error_type": type(exc).__name__}),
            file=sys.stderr,
        )
    print("{}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "session-start", "stop", "bind", "status"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--conversation-id")
    parser.add_argument("--campaign")
    parser.add_argument("--slot", choices=["am", "pm"])
    parser.add_argument("--owner")
    parser.add_argument("--round-id")
    parser.add_argument("--lease-generation", type=int)
    args = parser.parse_args()
    if args.action in {"session-start", "stop"}:
        _hook(args.action, args.root)
        return
    if args.action == "install":
        print(json.dumps({
            "configuration": str(install_hooks(args.root)), "native_trigger_observed": False,
        }))
        return
    service = AntigravityCampaignEvents(args.root)
    if not args.conversation_id:
        parser.error("--conversation-id must come from this native session")
    if args.action == "bind":
        binding = CampaignHostBinding(
            campaign_id=args.campaign, slot=args.slot, owner=args.owner,
            round_id=args.round_id, lease_generation=args.lease_generation,
        )
        result: object = {
            "binding": service.bind(args.conversation_id, binding),
            "business_continuation_allowed": True,
        }
    else:
        session = service.get(args.conversation_id)
        result = None if session is None else session.model_dump(mode="json")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
