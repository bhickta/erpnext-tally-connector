param(
    [string]$ConfigPath = "",
    [string]$Branch = "",
    [string]$FrappeUrl = ""
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$python = Join-Path $root "python.exe"
$pythonw = Join-Path $root "pythonw.exe"
$existing = @(Get-CimInstance Win32_Process -Filter "Name='ERPNextTallyControlCentre.exe' OR Name='pythonw.exe'" | Where-Object {
    $_.Name -eq 'ERPNextTallyControlCentre.exe' -or ($_.CommandLine -and $_.CommandLine -match 'express_tally\.bridge')
})
if (-not $ConfigPath -and $existing.Count -gt 0) {
    if ($existing[0].CommandLine -match '--config\s+"([^"]+)"') { $ConfigPath = $Matches[1] }
    elseif ($existing[0].CommandLine -match '--config\s+(\S+)') { $ConfigPath = $Matches[1] }
}
if (-not $ConfigPath) {
    $ConfigPath = Join-Path $env:LOCALAPPDATA "Express Tally Control Centre\tally-bridge.json"
}
if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Bridge configuration not found: $ConfigPath" }
$interactiveUser = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $interactiveUser) { throw "Sign in to the Windows desktop before installing the bridge" }
Push-Location $root
try {
    & $python -m express_tally.bridge self-test
    if ($LASTEXITCODE -ne 0) { throw "Portable bridge self-test failed" }
    & $python -m unittest express_tally.tests.test_bridge express_tally.tests.test_payments express_tally.tests.test_ledger_bridge
    if ($LASTEXITCODE -ne 0) { throw "Portable bridge regression tests failed" }
} finally { Pop-Location }

$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$backup = "$ConfigPath.backup-$(Get-Date -Format yyyyMMddHHmmss)"
Copy-Item -LiteralPath $ConfigPath -Destination $backup
if ($FrappeUrl) { $config.frappe_url = $FrappeUrl }
# A masters-sync update preserves the operator's flow selection. Keep
# unattended synchronization off until they review the new UI.
$config.auto_sync_enabled = $false
$config.open_browser_on_start = $false
$utf8 = New-Object System.Text.UTF8Encoding($false)
# Run in the signed-in desktop rather than the guest-agent SYSTEM account.
$taskName = 'Express-Tally-Portable-Control-Centre'
$arguments = '-m express_tally.bridge --config "{0}" serve --no-browser' -f $ConfigPath
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId $interactiveUser -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $interactiveUser
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Force | Out-Null
[IO.File]::WriteAllText($ConfigPath, ($config | ConvertTo-Json -Depth 30), $utf8)
foreach ($process in $existing) { Stop-Process -Id $process.ProcessId -Force }
# Disable the old startup launcher after the new package has passed its tests.
$userName = ($interactiveUser -split '\\')[-1]
$oldStartup = "C:\Users\$userName\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\Express Tally Control Centre.cmd"
if (Test-Path -LiteralPath $oldStartup) { Move-Item -LiteralPath $oldStartup -Destination "$oldStartup.disabled" -Force }
Start-ScheduledTask -TaskName $taskName
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        # The local service is healthy even when Tally or ERPNext is
        # temporarily unavailable. Connection state is shown in the UI.
        $state = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.listen_port)/api/v1/state" -TimeoutSec 5
        if ($state.status -eq 1) { Write-Output "Updated bridge running. Configuration backup: $backup"; exit 0 }
    } catch { }
}
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Copy-Item -LiteralPath $backup -Destination $ConfigPath -Force
if (Test-Path -LiteralPath "$oldStartup.disabled") { Move-Item -LiteralPath "$oldStartup.disabled" -Destination $oldStartup -Force }
if ($existing.Count -gt 0) {
    $rollbackAction = New-ScheduledTaskAction -Execute $existing[0].ExecutablePath -Argument ('--config "{0}" serve --no-browser' -f $ConfigPath)
    $rollbackTask = 'Express-Tally-Bridge-Rollback'
    Register-ScheduledTask -TaskName $rollbackTask -Action $rollbackAction -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $rollbackTask
    Start-Sleep -Seconds 2
    Unregister-ScheduledTask -TaskName $rollbackTask -Confirm:$false | Out-Null
}
throw "Updated bridge did not become healthy; restored the previous configuration and bridge"
