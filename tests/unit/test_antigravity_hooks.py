"""Antigravity protocol fixtures exercise existing campaign lease/checkpoint paths."""
from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.investor_orchestration import antigravity_hooks
from astock.investor_orchestration.antigravity_hooks import (
    AntigravityCampaignEvents,
    CampaignHostBinding,
    install_hooks,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.watch_campaign import WatchCampaignService

CONVERSATION = "6ae67eaf-fd46-4dbb-8a89-e3031c2322f7"


@pytest.fixture
def native(tmp_path: Path):
    database = tmp_path / "runtime/state.sqlite"
    InvestorOrchestrationStore(database).initialize()
    campaign = WatchCampaignService(database, "native-observer-fixture")
    campaign.initialize(["XSHG:603317"])
    service = AntigravityCampaignEvents(tmp_path)
    payload: dict[str, object] = {"conversationId": CONVERSATION, "workspacePaths": [str(tmp_path)]}
    service.session_start(payload)
    begun = campaign.begin("am", "2026-11-21", "native-owner")
    binding = CampaignHostBinding(
        campaign_id=campaign.campaign_id, slot="am", owner=begun.owner,
        round_id=begun.round_id, lease_generation=begun.lease_generation,
    )
    assert service.bind(CONVERSATION, binding) == "BOUND"
    return service, campaign, binding, payload


def _stop(payload: dict[str, object], reason: str, *, idle: bool = True, execution: int = 1):
    return {**payload, "executionNum": execution, "terminationReason": reason, "fullyIdle": idle}


@pytest.mark.parametrize("reason", ["max_steps_exceeded", "error"])
def test_real_interruption_checkpoints_same_campaign_lease_without_completing(
    native, reason,
) -> None:
    service, campaign, binding, payload = native
    before = campaign.load().rounds["am"]
    event = _stop(payload, reason)
    assert service.stop(event) == "APPLIED"
    after = campaign.load().rounds["am"]
    assert after.status == "PARTIAL"
    assert after.owner == ""
    assert after.round_id == binding.round_id
    assert after.lease_generation == binding.lease_generation
    assert after.deadline_at == before.deadline_at
    assert after.completed_units == before.completed_units == 0
    assert after.checkpoint == before.checkpoint
    assert service.stop(event) == "APPLIED"
    assert campaign.load().rounds["am"] == after


@pytest.mark.parametrize(("reason", "idle"), [
    ("model_stop", True), ("unknown_stop_reason", True), ("error", False),
])
def test_observation_never_releases_active_work_or_fakes_completion(native, reason, idle) -> None:
    service, campaign, _, payload = native
    before = campaign.load()
    assert service.stop(_stop(payload, reason, idle=idle)) == "OBSERVED"
    assert campaign.load() == before


def test_background_stop_can_become_idle_without_new_execution_identity(native) -> None:
    service, campaign, _, payload = native
    assert service.stop(_stop(payload, "error", idle=False)) == "OBSERVED"
    assert campaign.load().rounds["am"].owner == "native-owner"
    assert service.stop(_stop(payload, "error", idle=True)) == "APPLIED"
    assert service.stop(_stop(payload, "error", idle=False)) == "STALE_EVENT"


def test_old_session_binding_cannot_release_new_round_and_does_not_block_business(native) -> None:
    service, campaign, binding, payload = native
    assert service.stop(_stop(payload, "max_steps_exceeded")) == "APPLIED"
    successor = campaign.begin("am", "2026-11-21", "successor")
    assert successor.lease_acquired
    new_binding = binding.model_copy(update={
        "owner": successor.owner, "lease_generation": successor.lease_generation,
    })
    assert service.bind(CONVERSATION, new_binding) == "ROUND_NOT_BOUND"
    assert service.stop(_stop(payload, "error", execution=2)) == "STALE_LEASE"
    assert campaign.load().rounds["am"].owner == "successor"
    assert campaign.load().rounds["am"].status == "PENDING"


def test_expired_lease_and_unobserved_session_are_nonmutating(native) -> None:
    service, campaign, binding, payload = native
    old = campaign.load()
    expired = old.rounds["am"].model_copy(update={
        "deadline_at": datetime.now(UTC) - timedelta(seconds=1),
    })
    campaign._save(old.model_copy(update={"rounds": {"am": expired}}))
    assert service.stop(_stop(payload, "error")) == "STALE_LEASE"
    assert campaign.load().rounds["am"] == expired
    assert service.bind("c4c854d5-4a62-419b-bdee-b2401e6ed598", binding) == "SESSION_NOT_OBSERVED"


def test_native_metadata_does_not_store_private_prompt_error_or_transcript(native) -> None:
    service, _, _, payload = native
    event = {
        **_stop(payload, "unrecognized-private-reason"),
        "lastUserInput": "private prompt",
        "error": "private provider body",
        "transcriptPath": "private transcript path",
        "artifactDirectoryPath": "private artifact path",
    }
    service.stop(event)
    session = service.get(CONVERSATION)
    assert session is not None
    frozen = session.model_dump_json()
    assert "private" not in frozen
    assert session.last_stop is not None
    assert session.last_stop.reason == "UNKNOWN"


def test_wrong_workspace_and_older_execution_cannot_mutate_campaign(native, tmp_path: Path) -> None:
    service, campaign, _, payload = native
    assert service.stop(_stop(payload, "model_stop", execution=2)) == "OBSERVED"
    assert service.stop(_stop(payload, "error", execution=1)) == "STALE_EVENT"
    before = campaign.load()
    with pytest.raises(ValueError, match="different workspace"):
        service.stop({**_stop(payload, "error"), "workspacePaths": [str(tmp_path / "different")]})
    assert campaign.load() == before


def test_hook_cli_always_returns_empty_object_and_does_not_inject_continue(
    native, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    service, _, _, payload = native
    monkeypatch.setattr(sys, "argv", ["native-hook", "stop", "--root", str(service.root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_stop(payload, "model_stop"))))
    antigravity_hooks.main()
    assert capsys.readouterr().out == "{}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json with private details"))
    antigravity_hooks.main()
    output = capsys.readouterr()
    assert output.out == "{}\n"
    assert "private" not in output.err
    assert "continue" not in output.out


def test_install_merges_existing_hooks_and_is_idempotent(tmp_path: Path) -> None:
    executable = tmp_path / ".venv" / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture executable identity")
    config = tmp_path / ".agents" / "hooks.json"
    config.parent.mkdir()
    original = {
        "other-hook": {"Stop": [{"command": "other-command"}]},
        "astock-campaign-observer": {
            "enabled": False, "Stop": [{"command": "retain-user-handler"}],
        },
    }
    config.write_text(json.dumps(original), encoding="utf-8")
    assert install_hooks(tmp_path, python=executable) == config
    first = config.read_bytes()
    merged = json.loads(first)
    assert merged["other-hook"] == original["other-hook"]
    own = merged["astock-campaign-observer"]
    assert own["enabled"] is False
    assert own["Stop"][0] == {"command": "retain-user-handler"}
    assert len(own["Stop"]) == 2
    assert str(executable) in own["Stop"][1]["command"]
    assert str(tmp_path) in own["Stop"][1]["command"]
    assert own["Stop"][1]["timeout"] == 10
    install_hooks(tmp_path, python=executable)
    assert config.read_bytes() == first


def test_install_rejects_wrong_interpreter_without_changing_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="project's own Python"):
        install_hooks(tmp_path, python=Path(sys.executable))
    assert not (tmp_path / ".agents/hooks.json").exists()

def test_long_normal_stop_cannot_enter_interruption_policy(
    native, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, campaign, _, payload = native
    before = campaign.load()
    long_round = before.rounds["am"].model_copy(update={
        "invocation_started_at": datetime.now(UTC) - timedelta(hours=3),
    })
    campaign._save(before.model_copy(update={"rounds": {"am": long_round}}))

    def reject_policy(*_args, **_kwargs):
        pytest.fail("normal Stop must not enter native interruption policy")

    monkeypatch.setattr(antigravity_hooks.HostContinuationPolicy, "evaluate", reject_policy)
    assert service.stop(_stop(payload, "model_stop")) == "OBSERVED"
    assert campaign.load().rounds["am"] == long_round


def test_session_start_exposes_only_validated_binding_identity(
    native, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    service, _, _, payload = native
    monkeypatch.setattr(sys, "argv", ["native-hook", "session-start", "--root", str(service.root)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        **payload, "lastUserInput": "private prompt", "error": "private provider response",
        "transcriptPath": "private transcript",
    })))
    antigravity_hooks.main()
    output = json.loads(capsys.readouterr().out)
    assert list(output) == ["injectSteps"]
    message = output["injectSteps"][0]["systemMessage"]["systemMessage"]
    metadata = json.loads(message.split(": ", 1)[1])
    assert metadata == {"conversationId": CONVERSATION, "workspaceRoot": str(service.root)}
    assert "private" not in message
    assert "decision" not in output and "terminationBehavior" not in output
