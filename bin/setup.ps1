# =============================================================================
# setup.ps1 — one-time environment setup for guarded-eks-upgrade-agent (PowerShell)
#
# Creates a Python 3.13 virtualenv (CrewAI supports 3.10-3.13, NOT 3.14),
# installs dependencies, and runs a safe smoke check.
#
# Usage (from repo root, in PowerShell):
#     ./bin/setup.ps1
# =============================================================================
$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AppDir   = Join-Path $RepoRoot "app"
$VenvDir  = Join-Path $AppDir "venv"
$VenvPy   = Join-Path $VenvDir "Scripts\python.exe"

Write-Host "==> guarded-eks-upgrade-agent setup"

# 1. Pick a CrewAI-compatible Python (3.13 preferred).
$PyVer = $null
foreach ($v in @("3.13", "3.12", "3.11")) {
    & py "-$v" --version *> $null
    if ($LASTEXITCODE -eq 0) { $PyVer = $v; Write-Host "==> using Python $v"; break }
}
if (-not $PyVer) {
    Write-Error "No Python 3.11/3.12/3.13 found (CrewAI does not support 3.14). Install one from python.org."
    exit 1
}

# 2. Create the venv.
if (Test-Path $VenvDir) {
    Write-Host "==> venv already exists (reusing)"
} else {
    Write-Host "==> creating venv..."
    Push-Location $AppDir; & py "-$PyVer" -m venv venv; Pop-Location
}

# 3. Install deps.
Write-Host "==> installing dependencies (this can take a few minutes)..."
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r (Join-Path $AppDir "requirements.txt")

# 4. Ensure a .env exists.
if (-not (Test-Path (Join-Path $AppDir ".env"))) {
    Copy-Item (Join-Path $AppDir ".env.example") (Join-Path $AppDir ".env")
    Write-Host "==> created app/.env from .env.example - EDIT IT before running the agent."
}

# 5. Safe smoke check.
Write-Host "==> smoke check: python main.py --status"
Push-Location $AppDir; & $VenvPy main.py --status; Pop-Location

Write-Host ""
Write-Host "============================================================"
Write-Host "Setup complete."
Write-Host "Activate the venv with:  app\venv\Scripts\activate"
Write-Host "Next:"
Write-Host "  1. Edit app\.env (EKS_CLUSTER_NAME, AWS_REGION, TERRAFORM_DIR, CREWAI_LLM + key)"
Write-Host "  2. aws eks update-kubeconfig --name <cluster> --region <region>"
Write-Host "  3. ./bin/run.ps1 --target-version <current+1>"
Write-Host "============================================================"
