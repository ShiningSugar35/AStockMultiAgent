"""Real Windows PowerShell execution with isolated Scheduler/legacy-uv boundaries."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(
    os.name != "nt" or POWERSHELL is None, reason="requires Windows PowerShell"
)


def _literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_script(
    tmp_path: Path, script: str, arguments: str = "", *, setup: str = "", repeat: int = 1
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    """Never touch the machine Task Scheduler or run the old installer's uv."""
    observation = tmp_path / "powershell-observation.json"
    harness = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$global:RecordedCalls = New-Object System.Collections.ArrayList
$global:ExistingTask = $null
function uv {
    [void]$global:RecordedCalls.Add(@{command='uv'; arguments=@($args)})
    $global:LASTEXITCODE = 0
    '{}'
}
function Get-ScheduledTask { param($TaskName, $TaskPath, $ErrorAction)
    $global:ExistingTask
}
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory)
    [pscustomobject]@{Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger {
    param([switch]$Daily, [switch]$Once, $At, $RepetitionInterval, $RepetitionDuration)
    [pscustomobject]@{StartBoundary=[string]$At; DaysInterval=1; Enabled=$true;
        Repetition=[pscustomobject]@{Interval='PT10M'; Duration='P1D'}
    }
}
function New-ScheduledTaskSettingsSet {
    param($MultipleInstances, [switch]$StartWhenAvailable, $ExecutionTimeLimit)
    [pscustomobject]@{MultipleInstances=$MultipleInstances; Enabled=$true}
}
function Register-ScheduledTask {
    param($TaskName, $TaskPath, $Action, $Trigger, $Settings, $Description, [switch]$Force)
    [void]$global:RecordedCalls.Add(@{command='register'; task_name=$TaskName;
        action=$Action; description=$Description; force=[bool]$Force})
    $global:ExistingTask = [pscustomobject]@{
        TaskName=$TaskName; TaskPath='\'; Actions=@($Action); Triggers=@($Trigger);
        Description=$Description; Settings=$Settings
    }
    $global:ExistingTask
}
function Unregister-ScheduledTask {
    param($TaskName, $TaskPath, $Confirm)
    [void]$global:RecordedCalls.Add(@{command='unregister'; task_name=$TaskName})
    $global:ExistingTask=$null
}
"""
    harness += "\n" + setup + "\n"
    harness += "$errorText=$null; $exitCode=0; $output=@()\ntry {\n"
    for _ in range(repeat):
        harness += f"$output=@(& {_literal(ROOT / 'scripts' / script)} {arguments})\n"
    harness += "} catch { $errorText=$_.Exception.Message; $exitCode=1 }\n"
    harness += (
        "[pscustomobject]@{calls=@($global:RecordedCalls); output=$output; error=$errorText} | "
        "ConvertTo-Json -Depth 12 | "
        f"Set-Content -LiteralPath {_literal(observation)} -Encoding UTF8\n"
        "exit $exitCode\n"
    )
    encoded = base64.b64encode(harness.encode("utf-16-le")).decode("ascii")
    assert POWERSHELL is not None
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert observation.is_file(), (result.stdout, result.stderr)
    return result, json.loads(observation.read_text(encoding="utf-8-sig"))


def _json_output(observation: dict[str, Any]) -> dict[str, Any]:
    return json.loads("\n".join(str(part) for part in observation["output"]))


def test_installer_whatif_has_no_initialization_or_binding_side_effects(tmp_path: Path) -> None:
    result, observed = _run_script(tmp_path, "install_investor_tracking_task.ps1", "-WhatIf")
    assert result.returncode == 0, observed
    assert observed["calls"] == []


def test_uninstaller_whatif_does_not_claim_removal(tmp_path: Path) -> None:
    result, observed = _run_script(
        tmp_path,
        "uninstall_investor_tracking_task.ps1",
        "-WhatIf",
        setup="$global:ExistingTask=[pscustomobject]@{TaskName='AStockMultiAgent-InvestorTracking'}",
    )
    assert result.returncode == 0, observed
    assert observed["calls"] == []
    assert _json_output(observed)["Removed"] is False


def test_installer_missing_consent_cannot_register_scheduler_task(tmp_path: Path) -> None:
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        "-BindingId never-consented-test-binding "
        f"-Database {_literal(tmp_path / 'missing.sqlite')}",
    )
    assert result.returncode != 0, observed
    assert not any(call["command"] == "register" for call in observed["calls"])


def _binding_database(tmp_path: Path, **updates: Any) -> tuple[Path, str]:
    from datetime import UTC, datetime, timedelta

    import yaml

    from astock.investor_orchestration.models import ScheduledTaskBinding
    from astock.investor_orchestration.scheduled import policy_from_config
    from astock.investor_orchestration.store import InvestorOrchestrationStore

    path = tmp_path / "state.sqlite"
    store = InvestorOrchestrationStore(path)
    store.initialize()
    policy = policy_from_config(
        yaml.safe_load(
            (ROOT / "configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
        )
    )
    store.save_scheduled_policy(policy)
    binding = ScheduledTaskBinding(
        binding_id="installer's isolated binding",
        creation_mode="LOCAL_ONLY",
        execution_surface="LOCAL_DAEMON",
        schedule_expression="A_SHARE_PRE_OPEN_INTRADAY_POST_CLOSE",
        timezone=policy.market_timezone,
        policy_id=policy.policy_id,
        policy_hash=policy.policy_hash,
        active=True,
        confirmed_at=datetime.now(UTC) - timedelta(minutes=1),
        consent_hash="isolated-explicit-test-consent",
    ).model_copy(update=updates)
    store.save_binding(binding)
    return path, binding.binding_id


def _install_arguments(path: Path, binding: str) -> str:
    return f"-Database {_literal(path)} -BindingId {_literal(binding)}"


def test_installer_validates_existing_binding_without_modifying_database(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    before = path.read_bytes()
    result, observed = _run_script(
        tmp_path, "install_investor_tracking_task.ps1", _install_arguments(path, binding)
    )
    assert result.returncode == 0, (observed, result.stderr)
    assert path.read_bytes() == before
    assert [call["command"] for call in observed["calls"]] == ["register"]
    output = _json_output(observed)
    assert output["Registered"] is True
    assert output["ExecutionScope"] == "SCHEDULE_TICK_ONLY"
    assert output["LiveSourceAcquisitionEnabled"] is False
    assert output["SemanticWorkerCreated"] is False
    assert output["ResearchCoverageCertified"] is False
    assert output["ActualExecutionAllowed"] is False
    action = observed["calls"][0]["action"]
    command = base64.b64decode(action["Arguments"].split()[-1]).decode("utf-16-le")
    assert "uv run" not in command and "ExecutionPolicy Bypass" not in action["Arguments"]
    assert "schedule-source-prepare-all" not in command
    assert "exit $LASTEXITCODE" in command
    assert _literal(path) in command and _literal(binding) in command
    assert str(ROOT / ".venv/Scripts/python.exe") in command
    assert action["WorkingDirectory"] == str(ROOT)


def test_installer_live_source_acquisition_is_explicit_and_reported(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        _install_arguments(path, binding) + " -EnableLiveSourceAcquisition",
    )
    assert result.returncode == 0, (observed, result.stderr)
    assert [call["command"] for call in observed["calls"]] == ["register"]
    output = _json_output(observed)
    assert output["ExecutionScope"] == "SCHEDULE_TICK_AND_SOURCE_PREPARATION"
    assert output["LiveSourceAcquisitionEnabled"] is True
    assert output["SemanticWorkerCreated"] is False
    assert output["ResearchCoverageCertified"] is False
    assert output["ActualExecutionAllowed"] is False
    action = observed["calls"][0]["action"]
    command = base64.b64decode(action["Arguments"].split()[-1]).decode("utf-16-le")
    assert "schedule-tick" in command
    assert "schedule-source-prepare-all" in command
    assert "--live" in command
    assert "$tickExitCode -ne 0 -and $tickExitCode -ne 3" in command
    assert _literal(path) in command and _literal(binding) in command


@pytest.mark.parametrize("mutation", ["consent", "inactive", "future", "surface", "policy"])
def test_installer_rejects_unauthorized_or_stale_binding(tmp_path: Path, mutation: str) -> None:
    from datetime import UTC, datetime, timedelta

    updates: dict[str, dict[str, Any]] = {
        "consent": {"consent_hash": None},
        "inactive": {"active": False},
        "future": {"confirmed_at": datetime.now(UTC) + timedelta(days=1)},
        "surface": {"execution_surface": "WEB_CLOUD"},
        "policy": {"policy_hash": "different-policy"},
    }
    path, binding = _binding_database(tmp_path, **updates[mutation])
    before = path.read_bytes()
    result, observed = _run_script(
        tmp_path, "install_investor_tracking_task.ps1", _install_arguments(path, binding)
    )
    assert result.returncode != 0
    assert observed["calls"] == []
    assert path.read_bytes() == before


def test_installer_refuses_existing_foreign_task_without_force(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        _install_arguments(path, binding),
        setup=(
            "$global:ExistingTask=[pscustomobject]@{"
            "TaskName='AStockMultiAgent-InvestorTracking'; Description='another application'}"
        ),
    )
    assert result.returncode != 0
    assert observed["calls"] == []
    assert "not overwritten" in observed["error"]


def test_installer_register_failure_is_not_a_success_receipt(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        _install_arguments(path, binding),
        setup="function Register-ScheduledTask { throw 'injected registration failure' }",
    )
    assert result.returncode != 0
    assert observed["output"] == []
    assert "injected registration failure" in observed["error"]


def test_uninstaller_missing_task_reports_no_removal(tmp_path: Path) -> None:
    result, observed = _run_script(tmp_path, "uninstall_investor_tracking_task.ps1")
    assert result.returncode == 0, observed
    assert observed["calls"] == []
    assert _json_output(observed)["Removed"] is False


def test_uninstaller_does_not_hide_scheduler_permission_failure(tmp_path: Path) -> None:
    result, observed = _run_script(
        tmp_path,
        "uninstall_investor_tracking_task.ps1",
        setup="function Get-ScheduledTask { throw 'access denied' }",
    )
    assert result.returncode != 0
    assert observed["calls"] == []
    assert observed["output"] == []


def test_uninstaller_removes_owned_task_and_verifies_result(tmp_path: Path) -> None:
    result, observed = _run_script(
        tmp_path,
        "uninstall_investor_tracking_task.ps1",
        setup=(
            "$global:ExistingTask=[pscustomobject]@{TaskName='AStockMultiAgent-InvestorTracking'; "
            "Description='AStockMultiAgent controlled local tracking; identity=test'}"
        ),
    )
    assert result.returncode == 0, observed
    assert [call["command"] for call in observed["calls"]] == ["unregister"]
    assert _json_output(observed)["Removed"] is True
    assert _json_output(observed)["LocalBindingUnchanged"] is True


def test_uninstaller_detects_silent_failed_removal(tmp_path: Path) -> None:
    result, observed = _run_script(
        tmp_path,
        "uninstall_investor_tracking_task.ps1",
        setup="""$global:ExistingTask=[pscustomobject]@{
    TaskName='AStockMultiAgent-InvestorTracking'
    Description='AStockMultiAgent controlled local tracking; identity=test'
}
function Unregister-ScheduledTask { }""",
    )
    assert result.returncode != 0
    assert observed["output"] == []
    assert "could not be verified" in observed["error"]


def test_installer_repeat_is_idempotent_and_keeps_original_consent(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    before = path.read_bytes()
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        _install_arguments(path, binding),
        repeat=2,
    )
    assert result.returncode == 0, (observed, result.stderr)
    assert [call["command"] for call in observed["calls"]] == ["register"]
    assert _json_output(observed)["Status"] == "ALREADY_REGISTERED"
    assert path.read_bytes() == before


def test_installer_preview_needs_no_venv_database_or_consent(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        f"-WhatIf -ProjectRoot {_literal(tmp_path)}",
    )
    assert result.returncode == 0, observed
    assert observed["calls"] == []
    assert _json_output(observed)["BindingValidated"] is False
    assert set(tmp_path.iterdir()) - before == {tmp_path / "powershell-observation.json"}


def test_installer_cannot_bind_database_outside_project(tmp_path: Path) -> None:
    outside = ROOT.parent / "uncreated-tracking-script-test.sqlite"
    result, observed = _run_script(
        tmp_path, "install_investor_tracking_task.ps1", f"-Database {_literal(outside)}"
    )
    assert result.returncode != 0
    assert observed["calls"] == []
    assert "inside the project" in result.stderr


def test_task_action_propagates_real_cli_failure_without_path_injection(tmp_path: Path) -> None:
    path, binding = _binding_database(tmp_path)
    result, observed = _run_script(
        tmp_path, "install_investor_tracking_task.ps1", _install_arguments(path, binding)
    )
    assert result.returncode == 0, (observed, result.stderr)
    action = observed["calls"][0]["action"]
    # Isolated fault injection: preserve the original fixture, then let the real
    # CLI encounter an empty runtime with no matching binding. Never use production state.
    path.rename(tmp_path / "preserved-fixture.sqlite")
    child = subprocess.run(
        [action["Execute"], *action["Arguments"].split()],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert child.returncode == 2, (child.stdout, child.stderr)
    assert "unknown or inactive binding" in child.stderr
    assert (tmp_path / ".tracking-task/tmp").is_dir()


@pytest.mark.parametrize("field", ["interval", "disabled"])
def test_installer_verifies_registered_trigger_and_enabled_state(
    tmp_path: Path, field: str
) -> None:
    path, binding = _binding_database(tmp_path)
    mutation = (
        "$global:ExistingTask.Triggers[0].Repetition.Interval='PT60M'"
        if field == "interval"
        else "$global:ExistingTask.Settings.Enabled=$false"
    )
    setup = (
        "function Get-ScheduledTask { if ($null -ne $global:ExistingTask) { "
        + mutation
        + " }; $global:ExistingTask }"
    )
    result, observed = _run_script(
        tmp_path,
        "install_investor_tracking_task.ps1",
        _install_arguments(path, binding),
        setup=setup,
    )
    assert result.returncode != 0
    assert observed["output"] == []
    assert "could not be verified" in observed["error"]
