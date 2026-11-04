@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$procs = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'ERPNextTallyControlCentre.exe' -or ($_.Name -eq 'pythonw.exe' -and $_.CommandLine -match 'express_tally.bridge') });" ^
  "if (-not $procs) { Write-Output 'Bridge is not running.'; exit /b 0 };" ^
  "$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force };" ^
  "Write-Output 'Bridge stopped.'"
if /i not "%~1"=="/quiet" pause
