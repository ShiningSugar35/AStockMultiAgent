[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [ValidateNotNullOrEmpty()]
    [string]$BindingId = "astock-three-domain-local",
    [ValidatePattern('^[^\\/\*\?\[\]\x00-\x1f]+$')]
    [string]$TaskName = "AStockMultiAgent-InvestorTracking",
    [string]$Database = "runtime/state.sqlite",
    [ValidateRange(5, 20)]
    [int]$IntervalMinutes = 10,
    [switch]$EnableLiveSourceAcquisition
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

# Preview must not initialize a database, manufacture consent or invoke a worker.
if (-not $PSCmdlet.ShouldProcess($TaskName, "Install clock for an existing consented local binding")) {
    [pscustomobject]@{
        TaskName = $TaskName
        Registered = $false
        Status = "PREVIEW_OR_DECLINED"
        BindingValidated = $false
        LiveSourceAcquisitionRequested = [bool]$EnableLiveSourceAcquisition
        LocalFactsPreserved = $true
    } | ConvertTo-Json
    return
}

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$config = Join-Path $ProjectRoot "configs\scheduled_investor_tracking_v1.yaml"
if (-not [IO.Path]::IsPathRooted($Database)) {
    $Database = Join-Path $ProjectRoot $Database
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project .venv Python is missing; install project dependencies explicitly first."
}

# Only read canonical metadata. No init, migration, binding creation or consent refresh.
$validation = @'
import json, sqlite3, sys
from contextlib import closing
from pathlib import Path
import yaml
from astock.investor_orchestration.models import ScheduledDomain, ScheduledTaskBinding, ScheduledResearchPolicy
from astock.investor_orchestration.scheduled import ScheduledResearchService, policy_from_config
from astock.investor_orchestration.utils import utc_now
root, database, config = (Path(value).resolve() for value in (sys.argv[1], sys.argv[2], sys.argv[4]))
if not all(path.is_relative_to(root) for path in (database, config, Path(sys.executable).resolve())):
    raise SystemExit('Task paths must remain inside the project')
if not database.is_file():
    raise SystemExit('Existing initialized database and explicit consented binding required')
with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
    row = connection.execute('SELECT payload_json,active FROM scheduled_task_bindings WHERE binding_id=?', (sys.argv[3],)).fetchone()
    if row is None:
        raise SystemExit('Existing explicit consented binding required; installer never grants consent')
    binding = ScheduledTaskBinding.model_validate_json(row[0])
    if not row[1] or not binding.active or not binding.consent_hash or not binding.consent_hash.strip() or binding.confirmed_at is None or binding.confirmed_at > utc_now():
        raise SystemExit('Binding requires active, non-future explicit consent')
    if binding.binding_id != sys.argv[3] or binding.execution_surface != 'LOCAL_DAEMON' or binding.creation_mode != 'LOCAL_ONLY':
        raise SystemExit('Binding is not authorized for LOCAL_ONLY/LOCAL_DAEMON')
    row = connection.execute('SELECT payload_json FROM scheduled_research_policies WHERE policy_id=? AND active=1 ORDER BY created_at DESC LIMIT 1', (binding.policy_id,)).fetchone()
    if row is None:
        raise SystemExit('Active scheduled policy is missing')
    policy = ScheduledResearchPolicy.model_validate_json(row[0])
raw = yaml.safe_load(config.read_text(encoding='utf-8'))
expected = policy_from_config(raw)
ScheduledResearchService._validate_policy(policy)
if policy != expected or binding.policy_hash != policy.policy_hash or binding.timezone != policy.market_timezone:
    raise SystemExit('Binding does not match the current policy; new explicit authorization required')
if set(policy.domains) != set(ScheduledDomain):
    raise SystemExit('The three-domain tracking policy is required')
print(json.dumps({'database': str(database), 'timezone': policy.market_timezone, 'policy_hash': policy.policy_hash, 'paper_policy': policy.action_policy_by_domain[ScheduledDomain.PAPER_HOLDING].value}))
'@
$validationBytes = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($validation))
$bootstrap = "import base64;exec(base64.b64decode('$validationBytes'))"
Push-Location -LiteralPath $ProjectRoot
try {
    $validated = & $python -X utf8 -B -c $bootstrap $ProjectRoot $Database $BindingId $config
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduled binding validation failed; no task was registered."
    }
    $metadata = ($validated | Out-String) | ConvertFrom-Json
}
finally {
    Pop-Location
}
$Database = $metadata.database

function ConvertTo-PSLiteral([string]$Value) {
    return "'" + $Value.Replace("'", "''") + "'"
}
$rootLiteral = ConvertTo-PSLiteral $ProjectRoot
$pythonLiteral = ConvertTo-PSLiteral $python
$databaseLiteral = ConvertTo-PSLiteral $Database
$bindingLiteral = ConvertTo-PSLiteral $BindingId
$configLiteral = ConvertTo-PSLiteral $config
$tempLiteral = ConvertTo-PSLiteral (Join-Path ([IO.Path]::GetDirectoryName($Database)) ".tracking-task\tmp")
$commandPrefix = @"
`$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $rootLiteral
[void][IO.Directory]::CreateDirectory($tempLiteral)
`$env:TEMP = $tempLiteral
`$env:TMP = $tempLiteral
`$env:TMPDIR = $tempLiteral
`$env:PYTHONDONTWRITEBYTECODE = '1'
"@
$commandPrefix += [Environment]::NewLine
if ($EnableLiveSourceAcquisition) {
    $command = $commandPrefix + @"
& $pythonLiteral -X utf8 -B -c 'from astock.cli import app; app()' investor schedule-tick $bindingLiteral --database $databaseLiteral --config-path $configLiteral
`$tickExitCode = `$LASTEXITCODE
if (`$tickExitCode -ne 0 -and `$tickExitCode -ne 3) { exit `$tickExitCode }
& $pythonLiteral -X utf8 -B -c 'from astock.cli import app; app()' investor schedule-source-prepare-all $bindingLiteral --live --database $databaseLiteral
exit `$LASTEXITCODE
"@
}
else {
    $command = $commandPrefix + @"
& $pythonLiteral -X utf8 -B -c 'from astock.cli import app; app()' investor schedule-tick $bindingLiteral --database $databaseLiteral --config-path $configLiteral
exit `$LASTEXITCODE
"@
}
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$powerShell = Join-Path $PSHOME "powershell.exe"
$argument = "-NoProfile -NonInteractive -EncodedCommand $encoded"
$identityBytes = [Text.Encoding]::UTF8.GetBytes(
    "$ProjectRoot`n$Database`n$BindingId`n$IntervalMinutes`n$([bool]$EnableLiveSourceAcquisition)"
)
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $identity = [BitConverter]::ToString($hasher.ComputeHash($identityBytes)).Replace("-", "").ToLowerInvariant()
}
finally {
    $hasher.Dispose()
}
$description = "AStockMultiAgent controlled local tracking; identity=$identity; no broker execution."
function Test-TrackingTaskDefinition([object]$Task) {
    if ($null -eq $Task -or $Task.Description -cne $description -or
        @($Task.Actions).Count -ne 1 -or @($Task.Triggers).Count -ne 1 -or
        $Task.Actions[0].Execute -cne $powerShell -or
        $Task.Actions[0].Arguments -cne $argument -or
        $Task.Actions[0].WorkingDirectory -cne $ProjectRoot -or
        -not $Task.Settings.Enabled -or -not $Task.Triggers[0].Enabled -or
        $Task.Triggers[0].DaysInterval -ne 1 -or
        [string]::IsNullOrWhiteSpace($Task.Triggers[0].StartBoundary)) {
        return $false
    }
    try {
        $interval = [Xml.XmlConvert]::ToTimeSpan($Task.Triggers[0].Repetition.Interval)
        $duration = [Xml.XmlConvert]::ToTimeSpan($Task.Triggers[0].Repetition.Duration)
        $start = [datetime]$Task.Triggers[0].StartBoundary
        return ($interval -eq (New-TimeSpan -Minutes $IntervalMinutes) -and
            $duration -eq (New-TimeSpan -Days 1) -and $start.TimeOfDay -eq [TimeSpan]::Zero)
    }
    catch {
        return $false
    }
}
$existing = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ceq $TaskName })
$status = "REGISTERED"
if ($existing.Count -gt 0) {
    if ($existing.Count -ne 1 -or -not (Test-TrackingTaskDefinition $existing[0])) {
        throw "Task name collision or changed configuration; existing task was not overwritten."
    }
    $status = "ALREADY_REGISTERED"
}
else {
    $action = New-ScheduledTaskAction -Execute $powerShell -Argument $argument -WorkingDirectory $ProjectRoot
    # Wake all day in the host timezone. Python alone owns Asia/Shanghai sessions.
    $trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
    $repeating = New-ScheduledTaskTrigger -Once -At "00:00" `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 1)
    $trigger.Repetition = $repeating.Repetition
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
    Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Description $description -ErrorAction Stop | Out-Null
    $installed = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ceq $TaskName })
    if ($installed.Count -ne 1 -or -not (Test-TrackingTaskDefinition $installed[0])) {
        throw "Task registration could not be verified; do not treat this run as successful."
    }
}
$executionScope = if ($EnableLiveSourceAcquisition) {
    "SCHEDULE_TICK_AND_SOURCE_PREPARATION"
}
else {
    "SCHEDULE_TICK_ONLY"
}
[pscustomobject]@{
    TaskName = $TaskName
    BindingId = $BindingId
    Registered = $true
    Status = $status
    ProjectRoot = $ProjectRoot
    IntervalMinutes = $IntervalMinutes
    MarketTimezone = $metadata.timezone
    PolicyHash = $metadata.policy_hash
    PaperPolicy = $metadata.paper_policy
    ActualExecutionAllowed = $false
    LocalFactsPreserved = $true
    ExecutionScope = $executionScope
    LiveSourceAcquisitionEnabled = [bool]$EnableLiveSourceAcquisition
    SemanticWorkerCreated = $false
    ResearchCoverageCertified = $false
} | ConvertTo-Json -Depth 4
