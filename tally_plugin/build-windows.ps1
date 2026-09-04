$ErrorActionPreference = "Stop"

$PluginDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $PluginDir "..")).Path
$BuildRoot = Join-Path $RepoRoot "build\tally-bridge-windows"
$ExeDir = Join-Path $BuildRoot "exe"
$WorkDir = Join-Path $BuildRoot "work"
$SpecDir = Join-Path $BuildRoot "spec"
$OutputZip = Join-Path $RepoRoot "dist\ERPNext-Tally-Control-Centre-Windows-x64.zip"

npm --prefix (Join-Path $RepoRoot "control-centre") run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m pip install --disable-pip-version-check --requirement (Join-Path $PluginDir "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name ERPNextTallyControlCentre `
    --paths $RepoRoot `
    --add-data "$(Join-Path $RepoRoot 'express_tally\bridge\web');express_tally\bridge\web" `
    --distpath $ExeDir `
    --workpath $WorkDir `
    --specpath $SpecDir `
    (Join-Path $PluginDir "windows_entry.py")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Executable = Join-Path $ExeDir "ERPNextTallyControlCentre.exe"
$SelfTest = Start-Process -FilePath $Executable -ArgumentList "self-test" -Wait -PassThru
if ($SelfTest.ExitCode -ne 0) { exit $SelfTest.ExitCode }

python (Join-Path $PluginDir "build_package.py") --output $OutputZip --executable $Executable
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$ChecksumFile = "$OutputZip.sha256"
$Hash = (Get-FileHash -Algorithm SHA256 $OutputZip).Hash.ToLowerInvariant()
Set-Content -Path $ChecksumFile -Value "$Hash  ERPNext-Tally-Control-Centre-Windows-x64.zip" -Encoding ascii

Write-Host "Built $OutputZip"
Write-Host "SHA256 $Hash"
