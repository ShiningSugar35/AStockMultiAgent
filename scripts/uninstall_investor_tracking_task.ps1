[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidatePattern('^[^\\/\*\?\[\]\x00-\x1f]+$')]
    [string]$TaskName = "AStockMultiAgent-InvestorTracking"
)

$ErrorActionPreference = "Stop"
if (-not $PSCmdlet.ShouldProcess($TaskName, "Unregister local investor tracking clock")) {
    [pscustomobject]@{
        TaskName = $TaskName
        Removed = $false
        Reason = "PREVIEW_OR_DECLINED"
        LocalFactsPreserved = $true
        LocalBindingUnchanged = $true
    } | ConvertTo-Json
    return
}
$existing = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ceq $TaskName })
if ($existing.Count -eq 0) {
    [pscustomobject]@{
        TaskName = $TaskName
        Removed = $false
        Reason = "NOT_FOUND"
        LocalFactsPreserved = $true
        LocalBindingUnchanged = $true
    } | ConvertTo-Json
    return
}
if ($existing.Count -ne 1 -or $existing[0].Description -notlike 'AStockMultiAgent controlled local tracking; identity=*') {
    throw "Task ownership is unknown; the existing task was not removed."
}
Unregister-ScheduledTask -TaskPath '\' -TaskName $TaskName -Confirm:$false -ErrorAction Stop
$remaining = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -ceq $TaskName })
if ($remaining.Count -ne 0) {
    throw "Task removal could not be verified."
}
[pscustomobject]@{
    TaskName = $TaskName
    Removed = $true
    Reason = "REMOVED"
    LocalFactsPreserved = $true
    LocalBindingUnchanged = $true
} | ConvertTo-Json
