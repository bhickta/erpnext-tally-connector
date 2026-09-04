@echo off
setlocal
cd /d "%~dp0"

if not exist "ERPNextTallyControlCentre.exe" (
  echo ERPNextTallyControlCentre.exe is missing.
  echo Download and extract the complete Windows package from the GitHub release.
  pause
  exit /b 2
)

start "" "ERPNextTallyControlCentre.exe"
