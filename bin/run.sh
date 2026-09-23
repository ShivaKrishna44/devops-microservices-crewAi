#!/usr/bin/env bash
# =============================================================================
# run.sh — run the agent through the project's venv (no manual activation).
#
# Passes all arguments straight to main.py. Examples:
#     bash bin/run.sh --status
#     bash bin/run.sh --target-version 1.34
#     bash bin/run.sh --apply --target-version 1.34
#     bash bin/run.sh --reset
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${REPO_ROOT}/app"
VENV_PY="${APP_DIR}/venv/Scripts/python.exe"

if [ ! -x "$VENV_PY" ]; then
  echo "ERROR: venv not found. Run 'bash bin/setup.sh' first."
  exit 1
fi

(cd "$APP_DIR" && "$VENV_PY" main.py "$@")
