#!/usr/bin/env bash
# =============================================================================
# setup.sh — one-time environment setup for guarded-eks-upgrade-agent
#
# Creates a Python 3.13 virtualenv (CrewAI supports 3.10–3.13, NOT 3.14),
# installs dependencies, and runs a safe smoke check.
#
# Usage (from repo root, in Git Bash):
#     bash bin/setup.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${REPO_ROOT}/app"
VENV_DIR="${APP_DIR}/venv"

echo "==> guarded-eks-upgrade-agent setup"

# 1. Pick a CrewAI-compatible Python (3.13 preferred, then 3.12/3.11).
PYEXE=""
for v in 3.13 3.12 3.11; do
  if py -"$v" --version >/dev/null 2>&1; then
    PYEXE="py -$v"
    echo "==> using Python $v"
    break
  fi
done
if [ -z "$PYEXE" ]; then
  echo "ERROR: no Python 3.11/3.12/3.13 found (CrewAI does not support 3.14)."
  echo "       Install one from python.org, then re-run."
  exit 1
fi

# 2. Create the venv.
if [ -d "$VENV_DIR" ]; then
  echo "==> venv already exists at $VENV_DIR (reusing)"
else
  echo "==> creating venv..."
  (cd "$APP_DIR" && $PYEXE -m venv venv)
fi

# 3. Install deps into the venv.
echo "==> installing dependencies (this can take a few minutes)..."
"$VENV_DIR/Scripts/python.exe" -m pip install --upgrade pip
"$VENV_DIR/Scripts/python.exe" -m pip install -r "${APP_DIR}/requirements.txt"

# 4. Ensure a .env exists (copy from example on first run).
if [ ! -f "${APP_DIR}/.env" ]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "==> created app/.env from .env.example — EDIT IT before running the agent."
fi

# 5. Safe smoke check (no cluster calls).
echo "==> smoke check: python main.py --status"
(cd "$APP_DIR" && "$VENV_DIR/Scripts/python.exe" main.py --status)

cat <<EOF

============================================================
Setup complete.

Activate the venv in future shells with:
    source app/venv/Scripts/activate

Next:
  1. Edit app/.env  (EKS_CLUSTER_NAME, AWS_REGION, TERRAFORM_DIR, CREWAI_LLM + key)
  2. aws eks update-kubeconfig --name <cluster> --region <region>
  3. bash bin/run.sh --target-version <current+1>   (pre-checks; abort at APPROVE to stay safe)
============================================================
EOF
