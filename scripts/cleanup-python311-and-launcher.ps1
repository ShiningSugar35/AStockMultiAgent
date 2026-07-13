[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator, then execute this script again.'
}

$python312 = 'D:\Environment\Python312\python.exe'
if (-not (Test-Path -LiteralPath $python312)) {
    throw "Safety check failed: retained Python was not found at $python312"
}
$version = (& $python312 --version 2>&1 | Out-String).Trim()
if ($version -notmatch '^Python 3\.12\.') {
    throw "Safety check failed: $python312 is not Python 3.12: $version"
}

$python311Products = @(
    '{0D289858-69D1-4CB6-946E-659F028DDC27}',
    '{25DC2A6F-FDC2-40D0-AA9D-3BF392BDF500}',
    '{55BEEF7A-9288-497D-B5CE-960D2F3C70A3}',
    '{5BF6CA5B-E057-413A-B87A-CCD47600E465}',
    '{611F1238-29A9-495F-B1F4-CFFCC98D9421}',
    '{9EB782CC-B2A5-4B67-BFEC-C91F5B755CAF}',
    '{A2BCB6C1-272D-437F-A5BC-92431FC521B4}',
    '{BA9ABB78-751C-4488-80A9-60E44290C060}',
    '{C321A7FC-E479-4E2A-AA09-2698EFEA4CA3}',
    '{D307D056-AF62-4F53-810E-052AAAF0EFB2}'
)
$launcherProduct = '{665A0435-D5D5-4A49-9DE0-FBC23C5425ED}'

foreach ($productCode in @($python311Products + $launcherProduct)) {
    $process = Start-Process -FilePath 'msiexec.exe' `
        -ArgumentList @('/x', $productCode, '/qn', '/norestart') `
        -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 1605, 1614, 3010)) {
        Write-Warning "Windows Installer returned $($process.ExitCode) for $productCode; verified orphan cleanup will continue."
    }
}

$orphanRegistryKeys = @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{c6a7d2cb-61ea-4f5e-bc56-95faa938bacf}',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\{665A0435-D5D5-4A49-9DE0-FBC23C5425ED}'
)
$orphanRegistryKeys += $python311Products | ForEach-Object {
    "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$_"
}
foreach ($key in $orphanRegistryKeys) {
    if (Test-Path -LiteralPath $key) {
        Remove-Item -LiteralPath $key -Recurse -Force
    }
}

$knownFiles = @('C:\Windows\py.exe', 'C:\Windows\pyw.exe')
foreach ($file in $knownFiles) {
    if (Test-Path -LiteralPath $file) {
        $item = Get-Item -LiteralPath $file
        if ($item.VersionInfo.InternalName -ne 'Python Launcher') {
            throw "Refusing to delete a file that is not Python Launcher: $file"
        }
        Remove-Item -LiteralPath $file -Force
    }
}

$knownDirectories = @(
    'C:\Users\hp\AppData\Local\Programs\Python\Python311',
    'C:\Users\hp\AppData\Local\Programs\Python\Launcher',
    'C:\Users\hp\AppData\Roaming\Python\Python311',
    'C:\Program Files\Python311',
    'C:\Program Files (x86)\Python311',
    'C:\Users\hp\AppData\Local\Package Cache\{c6a7d2cb-61ea-4f5e-bc56-95faa938bacf}'
)
foreach ($directory in $knownDirectories) {
    if (Test-Path -LiteralPath $directory) {
        $resolved = (Resolve-Path -LiteralPath $directory).Path
        if ($resolved -ne [IO.Path]::GetFullPath($directory)) {
            throw "Refusing to clean a directory with an unexpected resolved path: $directory -> $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$cleanUserPath = (($userPath -split ';') | Where-Object {
    $_ -and $_ -notmatch '(?i)Python311|Python\\Launcher'
}) -join ';'
[Environment]::SetEnvironmentVariable('Path', $cleanUserPath, 'User')

$remaining = Get-ItemProperty `
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*', `
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' `
    -ErrorAction SilentlyContinue | Where-Object {
        $_.DisplayName -match 'Python.*(3\.11|Launcher)|Launcher.*Python'
    }
if ($remaining) {
    $remaining | Select-Object DisplayName, DisplayVersion, PSPath | Format-List
    throw 'Python 3.11 or Python Launcher registration is still present.'
}
if (Get-Command py.exe -ErrorAction SilentlyContinue) {
    throw 'py.exe is still discoverable; open a new terminal and verify again.'
}

& $python312 --version
Write-Output 'Python 3.11 and Python Launcher remnants are clean; Python 3.12 remains available.'
