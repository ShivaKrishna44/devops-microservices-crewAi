# =============================================================================
# run.ps1 — run the agent through the project's venv (no manual activation).
#
# Passes all arguments straight to main.py. Examples:
#     ./bin/run.ps1 --status
#     ./bin/run.ps1 --target-version 1.34
#     ./bin/run.ps1 --apply --target-version 1.34
#     ./bin/run.ps1 --reset
# =============================================================================
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AppDir   = Join-Path $RepoRoot "app"
$VenvPy   = Join-Path $AppDir "venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Error "venv not found. Run './bin/setup.ps1' first."
    exit 1
}

Push-Location $AppDir
& $VenvPy main.py @args
$code = $LASTEXITCODE
Pop-Location
exit $code
