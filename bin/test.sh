#!/usr/bin/env bash
# =============================================================================
# test.sh — run the unit test suite (no cluster, no AWS, no CrewAI needed).
#
# Uses whatever Python is on PATH (the tests work on 3.14 too, via the tool
# shim). Install pytest first if needed: pip install pytest python-dotenv
#
# Usage (from repo root):
#     bash bin/test.sh
# =============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"
python -m pytest tests/ -v "$@"
